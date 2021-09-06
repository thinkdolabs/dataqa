import pandas as pd

import dataqa.db.ops.supervised as db_ops
import csv
import json

import dataqa.elasticsearch.client.utils.classification as classification_es
import dataqa.elasticsearch.client.utils.ner as ner_es
from dataqa.api.api_fns import utils
import dataqa.nlp.nlp_classification as nlp_classification
import dataqa.nlp.nlp_ner as nlp_ner
import dataqa.ml.sentiment as ml_sentiment
import numpy as np

import dataqa.ml.metrics.metrics as metrics
import dataqa.ml.metrics.ner as ner_metrics
from dataqa.api.api_fns.utils import get_column_names
from dataqa.constants import (ABSTAIN,
                              PROJECT_TYPE_CLASSIFICATION,
                              PROJECT_TYPE_NER, ES_GROUND_TRUTH_NAME_FIELD, SPACY_COLUMN_NAME)
import dataqa.db.ops.supervised as db
from dataqa.elasticsearch.client.utils import common as es
from dataqa.elasticsearch.client import queries
from dataqa.ml import distant_supervision as ds, sentiment as sentiment
from dataqa.ml.metrics.metrics import (get_doc_class_matrix,
                                       get_ground_truth_distribution_stats)
from dataqa.nlp import spacy_file_utils as spacy_file_utils


def parse_ner_regex_rule(rule):
    params = json.loads(rule.params)
    regex = params["regex"]
    match_num_entitities = int(params["n"])
    class_id = int(rule.class_id)
    return regex, match_num_entitities, class_id


def parse_ner_noun_phrase_regex_rule(rule):
    params = json.loads(rule.params)
    sentence = bool(params["sentence"])
    text_regex = params["text_regex"]
    noun_phrase_regex = params["noun_phrase_regex"]
    class_id = int(rule.class_id)
    return sentence, text_regex, noun_phrase_regex, class_id


def parse_classification_rule(rule):
    params = json.loads(rule.params)
    label = int(rule.class_id)
    rules = params['rules']
    negative_match = not (params['contains'])
    sentence_match = bool(params['sentence'])
    all_ordered_rules = []
    for item in rules:
        word = item['word']
        type_ = item['type']
        if type_.startswith('exact'):
            matcher = get_regex_matcher(type_, word, label)
        elif type_.startswith('token'):
            matcher = get_token_matcher(type_, word, label)
        elif type_.startswith('entity'):
            matcher = get_entity_matcher(type_, word, label)
        elif type_ == 'lemma':
            matcher = nlp_classification.get_lemma_matcher(word, label, ds.ABSTAIN, False)
        else:
            raise Exception(f"Rule type {type_} not supported.")

        all_ordered_rules.append(matcher)
    return all_ordered_rules, label, negative_match, sentence_match


def get_regex_matcher(type_, word, label):
    if type_ == 'exact case-sensitive':
        matcher = nlp_classification.get_regex_matcher_case_sensitive(word, label, ds.ABSTAIN, False)
    elif type_ == 'exact case-insensitive':
        matcher = nlp_classification.get_regex_matcher_case_insensitive(word, label, ds.ABSTAIN, False)
    else:
        raise Exception(f"Rule type {type_} not supported.")
    return matcher


def get_token_matcher(type_, word, label):
    if type_ == 'token case-sensitive':
        matcher = nlp_classification.get_token_matcher_case_sensitive(word, label, ds.ABSTAIN, False)
    elif type_ == 'token case-insensitive':
        matcher = nlp_classification.get_token_matcher_case_insensitive(word, label, ds.ABSTAIN, False)
    else:
        raise Exception(f"Rule type {type_} not supported.")
    return matcher


def get_entity_matcher(type_, word, label):
    try:
        entity_type = type_.split()[1].upper()
    except:
        raise Exception(f"Missing the entity type {type_}.")

    if entity_type not in ["PERSON", "NORP", "ORG", "GPE", "DATE", "TIME", "MONEY", "QUANTITY"]:
        raise Exception(f"Entity type {entity_type} from entity {type_} not supported.")

    matcher = nlp_classification.get_entity_matcher(word, entity_type, label, ds.ABSTAIN, False)
    return matcher


def create_ordered_match_lf(rule):
    all_ordered_rules, label, negative_match, sentence_match = parse_classification_rule(rule)
    lf = ds.ordered_match_lf(all_ordered_rules,
                             label,
                             rule.id,
                             negative_match,
                             sentence_match)
    return lf


def create_non_ordered_match_lf(rule):
    all_ordered_rules, label, negative_match, sentence_match = parse_classification_rule(rule)
    lf = ds.non_ordered_match_lf(all_ordered_rules,
                                 label,
                                 rule.id,
                                 negative_match,
                                 sentence_match)
    return lf


def create_sentiment_match_lf(rule):
    try:
        params = json.loads(rule.params)
        score = float(params['score'])
        is_gt = params['is_gt']
        label = int(rule.class_id)
        sentiment = params['sentiment']
        if sentiment not in ml_sentiment.SENTIMENT_COL_MAPPING.keys():
            raise Exception(f"Sentiment {sentiment} not supported.")
    except:
        raise Exception(f"Exception encountered when loading rule parameters: {rule.params}.")

    lf = ds.create_sentiment_lf(sentiment,
                                score,
                                is_gt,
                                label,
                                rule.id)
    return lf


def create_rule_index_row(row_ind, rule_ids, merged_labels, all_labels):
    """
    Creates an ES update doc for the rules, only for the rules that are non-abstain.
    """
    doc_changes = {}
    row_rules = all_labels[row_ind, :]

    if (row_rules != ABSTAIN).any():
        rules = [{"rule_id": int(x), "label": int(y)}
                 for x, y in zip(rule_ids, row_rules)
                 if int(y) != ABSTAIN]

        doc_changes["doc"] = {"rules": rules,
                              "predicted_label": int(merged_labels[row_ind])}

    return doc_changes


def create_rule_index_row_force_write(row_ind, rule_ids, merged_labels, all_labels):
    """
    Return update command for ES, forcing a write even if the fields ar empty.

    If the rules are empty, then so will the predicted_labels field.
    """
    doc_changes = create_rule_index_row(row_ind, rule_ids, merged_labels, all_labels)
    if not len(doc_changes):
        # delete both rules and predicted_labels field
        doc_changes = queries.script_delete_rules_predicted_label()

    return doc_changes


def index_labels(es_uri,
                 index_name,
                 merged_labels,
                 all_labels,
                 rule_ids,
                 doc_ids):
    batch_doc_size = 100
    num_points = len(doc_ids)
    rows = []
    selected_doc_ids = []
    for ind in range(num_points):
        if len(rows) and (len(rows) % batch_doc_size == 0):
            try:
                bulk_load_updates(es_uri, index_name, rows, selected_doc_ids)
                rows = []
                selected_doc_ids = []
            except:
                raise Exception(
                    f"Error bulk loading labels "
                    f"{doc_ids[0]} to {doc_ids[-1]} to elasticsearch")

        new_row = create_rule_index_row_force_write(ind, rule_ids, merged_labels, all_labels)

        if new_row:
            rows.append(new_row)
            if doc_ids:
                selected_doc_ids.append(doc_ids[ind])
            else:
                selected_doc_ids.append(ind)

    if rows:
        bulk_load_updates(es_uri, index_name, rows, selected_doc_ids)

    return


def bulk_load_updates(es_uri, index_name, list_labels, doc_ids):
    json_data = []
    for doc_id, doc in zip(doc_ids, list_labels):
        json_data.append(json.dumps({"update": {"_index": index_name,
                                                "_id": doc_id}}))
        json_data.append(json.dumps(doc))
    request_body = "\n".join(json_data) + "\n"
    es.bulk_upload(es_uri, request_body)


def index_spans_specific_docs(es_uri,
                              index_name,
                              merged_predicted_label_spans,
                              entity_spans,
                              rule_ids,
                              doc_ids_to_update):
    batch_doc_size = 100
    rows = []
    doc_ids = []
    for doc_id in doc_ids_to_update:
        if len(rows) and (len(rows) % batch_doc_size == 0):
            try:
                bulk_load_updates(es_uri, index_name, rows, doc_ids)
                rows = []
                doc_ids = []
            except:
                raise Exception(
                    f"Error bulk loading labels "
                    f"{doc_ids[0]} to {doc_ids[-1]} to elasticsearch")

        rules = [{"rule_id": rule_id, "label": spans}
                 for rule_id, spans in zip(rule_ids, entity_spans[doc_id]) if len(spans)]

        if len(rules):
            new_row = {"doc":
                           {"rules": rules,
                            "predicted_label": merged_predicted_label_spans[doc_id]}}
        else:
            new_row = queries.script_delete_rules_predicted_label()

        rows.append(new_row)
        doc_ids.append(doc_id)

    if len(rows):
        bulk_load_updates(es_uri, index_name, rows, doc_ids)

    return


def index_spans(es_uri,
                index_name,
                merged_predicted_label_spans,
                entity_spans,
                rule_ids):
    batch_doc_size = 100
    num_docs = len(entity_spans)
    rows = []
    doc_ids = []
    for ind in range(num_docs):
        if len(rows) and (len(rows) % batch_doc_size == 0):
            try:
                bulk_load_updates(es_uri, index_name, rows, doc_ids)
                rows = []
                doc_ids = []
            except:
                raise Exception(
                    f"Error bulk loading labels "
                    f"{doc_ids[0]} to {doc_ids[-1]} to elasticsearch")

        rules = [{"rule_id": rule_id, "label": spans}
                 for rule_id, spans in zip(rule_ids, entity_spans[ind]) if len(spans)]
        new_row = {"rules": rules,
                   "predicted_label": merged_predicted_label_spans[ind]}

        if len(rules):
            new_row = {"doc": new_row}
            rows.append(new_row)
            doc_ids.append(ind)

    if len(rows):
        bulk_load_updates(es_uri, index_name, rows, doc_ids)

    return


def get_rule_matrix_from_es_rules(docs, rule_ids):
    rules = [[doc["rules"].get(rule_id, ABSTAIN) for rule_id in rule_ids] for doc in docs]
    mat = np.array(rules)
    return mat


def fill_missing_value(label):
    if label is None:
        return ABSTAIN
    return label


def get_mats_from_es_docs(docs, rule_ids):
    all_labels_mat = get_rule_matrix_from_es_rules(docs, rule_ids)
    merged_labels = np.array([fill_missing_value(doc.get("predicted_label")) for doc in docs])
    manual_labels = np.array([fill_missing_value(doc.get("manual_label")) for doc in docs])
    return all_labels_mat, merged_labels, manual_labels


def get_ground_truth_mats_from_es_docs(entity_ids, docs):
    merged_labels = np.array([fill_missing_value(doc.get("predicted_label")) for doc in docs])
    ground_truth_labels = np.array([doc["ground_truth_label"] for doc in docs])

    ground_truth_labels_mat = get_doc_class_matrix(entity_ids, ground_truth_labels)
    merged_labels_mat = get_doc_class_matrix(entity_ids, merged_labels)

    return ground_truth_labels_mat, merged_labels_mat


def get_diff_rule_mats_after_adding_rule(es_uri,
                                         index_name,
                                         doc_ids,
                                         old_rule_ids,
                                         new_rules_mat,
                                         total_classes):
    docs = classification_es.get_specific_docs(es_uri, index_name, doc_ids)
    old_rule_labels_mat = get_rule_matrix_from_es_rules(docs, old_rule_ids)
    old_merged_labels = np.array([fill_missing_value(doc["predicted_label"]) for doc in docs])
    all_labels_mat = np.concatenate((old_rule_labels_mat, new_rules_mat), axis=1)
    new_merged_labels, merged_method = ds.merge_labels(all_labels_mat, total_classes)
    return (old_rule_labels_mat,
            old_merged_labels,
            all_labels_mat,
            new_merged_labels,
            merged_method)


def get_diff_rule_mats_after_deleting_rule(es_uri,
                                           index_name,
                                           rule_id_to_delete,
                                           rule_index_to_delete,
                                           old_rule_ids,
                                           total_classes,
                                           has_ground_truth_labels=False):
    docs, doc_ids = classification_es.get_docs_specific_rule(es_uri,
                                                             index_name,
                                                             rule_id_to_delete)
    old_rule_labels_mat = get_rule_matrix_from_es_rules(docs, old_rule_ids)
    old_merged_labels = np.array([fill_missing_value(doc["predicted_label"]) for doc in docs])
    manual_labels = np.array([fill_missing_value(doc["manual_label"]) for doc in docs])
    ground_truth_labels = None
    if has_ground_truth_labels:
        ground_truth_labels = np.array([doc["ground_truth_label"] for doc in docs])

    new_rule_labels_mat = np.delete(old_rule_labels_mat, rule_index_to_delete, axis=1)
    new_merged_labels, merged_method = ds.merge_labels(new_rule_labels_mat, total_classes)

    if len(new_merged_labels) == 0:
        new_merged_labels = np.ones((len(old_rule_labels_mat),)) * ABSTAIN

    return (old_rule_labels_mat,
            old_merged_labels,
            new_rule_labels_mat,
            new_merged_labels,
            merged_method,
            doc_ids,
            manual_labels,
            ground_truth_labels)


def get_new_rule_ids(project, rule_id):
    rule_ids = [rule.id for rule in project.rules]
    new_rule_ids = [rule.id for rule in project.rules if rule.id != rule_id]
    rule_index_to_delete = rule_ids.index(rule_id)
    return rule_index_to_delete, new_rule_ids


def delete_update_rule_stats_classification(project, es_uri, rule_id):
    """
    Delete a rule and update the statistics that are affected.

    Affected statistics: merged stats (coverage, conflicts, overlaps, accuracy).
    Rule stats: coverage, conflicts, overlaps. Rule accuracy stays the same.
    """
    rule_index_to_delete, new_rule_ids = get_new_rule_ids(project, rule_id)
    old_rule_ids = [rule.id for rule in project.rules]

    # 1. get all docs with specific rule_id (the one to delete)
    (old_rule_labels_mat,
     old_merged_labels,
     new_rule_labels_mat,
     new_merged_labels,
     merged_method,
     doc_ids,
     manual_labels,
     ground_truth_labels) = get_diff_rule_mats_after_deleting_rule(es_uri,
                                                                   project.index_name,
                                                                   rule_id,
                                                                   rule_index_to_delete,
                                                                   old_rule_ids,
                                                                   len(project.classes),
                                                                   project.has_ground_truth_labels)

    # Compute rule stats (no need to update accuracy)
    rule_stats = metrics.get_rule_stats_from_diff_classification(project,
                                                                 new_rule_labels_mat,
                                                                 new_merged_labels,
                                                                 new_rule_ids,
                                                                 old_rule_labels_mat,
                                                                 old_merged_labels,
                                                                 old_rule_ids)

    # Update the accuracy of the predicted_label (as the rules do not change)
    accuracy_stats = metrics.get_merged_accuracy_stats_from_diff_classification(project,
                                                                                new_merged_labels,
                                                                                old_merged_labels,
                                                                                manual_labels)

    if project.has_ground_truth_labels:
        total_predicted_entity_metrics = rule_stats["total_merged"]["entity_metrics"]
        ground_truth_stats = metrics.get_ground_truth_stats_from_diff_classification(project,
                                                                                     old_merged_labels,
                                                                                     new_merged_labels,
                                                                                     ground_truth_labels,
                                                                                     total_predicted_entity_metrics)
        accuracy_stats["ground_truth"] = ground_truth_stats

    # Update the rules and project db data
    db_ops.delete_rule(project, rule_index_to_delete)

    project.merged_method = merged_method
    db_ops.update_rule_stats_classification(project, rule_stats)
    db_ops.update_accuracy_stats_classification(project, accuracy_stats)

    # Update files and index (destructive ops)
    index_labels(es_uri,
                 project.index_name,
                 new_merged_labels,
                 new_rule_labels_mat,
                 new_rule_ids,
                 doc_ids)


def get_new_rule_spans_after_deletion(old_rule_spans, manual_label_spans, rule_index_to_delete):
    doc_ids_to_update = []
    new_rule_spans = []

    for doc_id, (doc_rule_spans, doc_manual_label_spans) \
            in enumerate(zip(old_rule_spans, manual_label_spans)):
        new_doc_spans = []
        for rule_index, doc_rule_span in enumerate(doc_rule_spans):
            if rule_index == rule_index_to_delete:
                if len(doc_rule_span):
                    doc_ids_to_update.append(doc_id)
            else:
                new_doc_spans.append(doc_rule_span)

        new_rule_spans.append(new_doc_spans)

    return new_rule_spans, doc_ids_to_update


def delete_update_rule_stats_ner(project, es_uri, rule_id):
    rule_index_to_delete, new_rule_ids = get_new_rule_ids(project, rule_id)
    old_rule_ids = [rule.id for rule in project.rules]
    entity_ids = [x.id for x in project.classes]

    # 1. get all the spans from ES
    old_rule_spans, predicted_label_spans, manual_label_spans = \
        ner_es.get_all_rule_or_manual_entity_spans(es_uri,
                                                   project.index_name,
                                                   old_rule_ids,
                                                   project.total_documents)

    # only need to update the documents that have the rule(s) we want to delete
    new_rule_spans, doc_ids_to_update = \
        get_new_rule_spans_after_deletion(old_rule_spans, manual_label_spans, rule_index_to_delete)

    # 2. get predicted_label
    merged_predicted_label_spans = nlp_ner.merge_spans_all_docs(new_rule_spans)

    # 3. compute stats on the rules (overlaps, coverage)
    rule_stats = ner_metrics.get_rule_entity_stats(new_rule_ids,
                                                   new_rule_spans,
                                                   merged_predicted_label_spans)

    accuracy_stats = ner_metrics.get_rule_accuracy_stats_ner(entity_ids,
                                                             new_rule_ids,
                                                             new_rule_spans,
                                                             merged_predicted_label_spans,
                                                             manual_label_spans)

    # 4. save stats to db
    # for each rule, save: total overlaps, coverage (total documents)
    db_ops.delete_rule(project, rule_index_to_delete)
    db.update_rules_project_ner(project, rule_stats)

    db_ops.update_rules_accuracy_project_ner(project, accuracy_stats)

    # 5. save predicted_label spans to ES
    index_spans_specific_docs(es_uri,
                              project.index_name,
                              merged_predicted_label_spans,
                              new_rule_spans,
                              new_rule_ids,
                              doc_ids_to_update)


def get_new_rule_labels_mat(df, new_rules):
    lfs = []
    for rule in new_rules:
        # create labelling function
        if rule.rule_type == 'ordered':
            lf = create_ordered_match_lf(rule)
        elif rule.rule_type == 'non-ordered':
            lf = create_non_ordered_match_lf(rule)
        elif rule.rule_type == 'sentiment':
            lf = create_sentiment_match_lf(rule)
        else:
            raise Exception("rule not supported")
            # load text
        lfs.append(lf)

    # apply the lfs
    rule_labels_mat = ds.apply_lfs(df, lfs)
    return rule_labels_mat


def apply_update_rules_classification(project, es_uri, df, rules):
    new_rule_ids = [rule.id for rule in rules]
    old_rule_ids = [rule.id for rule in project.rules if rule.id not in new_rule_ids]
    all_rule_ids = [rule.id for rule in project.rules]

    # get the new rules
    rule_labels_mat = get_new_rule_labels_mat(df, rules)
    doc_ids = sorted(set(np.where(rule_labels_mat != ABSTAIN)[0].tolist()))
    rule_labels_mat = rule_labels_mat[doc_ids, :]

    # download the old rules from ES
    (old_rule_labels_mat,
     old_merged_labels,
     new_rule_labels_mat,
     new_merged_labels,
     merged_method) = get_diff_rule_mats_after_adding_rule(es_uri,
                                                           project.index_name,
                                                           doc_ids,
                                                           old_rule_ids,
                                                           rule_labels_mat,
                                                           len(project.classes))

    # get overlaps/conflicts with and without the new rules
    rule_stats = metrics.get_rule_stats_from_diff_classification(project,
                                                                 new_rule_labels_mat,
                                                                 new_merged_labels,
                                                                 all_rule_ids,
                                                                 old_rule_labels_mat,
                                                                 old_merged_labels,
                                                                 old_rule_ids)

    project.merged_method = merged_method

    # update the db
    db_ops.update_rule_stats_classification(project, rule_stats)

    # save the new documents in ES
    index_labels(es_uri,
                 project.index_name,
                 new_merged_labels,
                 new_rule_labels_mat,
                 all_rule_ids,
                 doc_ids)


def create_regex_span_lf(regex,
                         match_num_entitities,
                         entity_id):
    fn = lambda df: nlp_ner.get_all_regex_entities(df,
                                                   regex,
                                                   match_num_entitities,
                                                   entity_id)
    return fn


def create_noun_phrase_regex_lf(sentence, text_regex, noun_phrase_regex, entity_id):
    fn = lambda df: nlp_ner.get_all_noun_phrase_regex_entities(df,
                                                               sentence,
                                                               text_regex,
                                                               noun_phrase_regex,
                                                               entity_id)
    return fn


def apply_rules_ner(df, entity_fns):
    all_entity_spans = []
    for entity_fn in entity_fns:
        entity_spans = entity_fn(df)
        all_entity_spans.append(entity_spans)
    all_entity_spans = [list(doc_rules) for doc_rules in (zip(*all_entity_spans))]
    return all_entity_spans


def get_new_spans(df, rules):
    """
    Returns a 3-level nested list where level 1: docs, level 2: rules and level 3: spans.

    So the output is a list:
    [spans_document_1, spans_document_2, ..., spans_document_N]
    where spans_document_i are all the rule spans for that document represented by a list:
    [rule_1, rule_2, ..., rule_R]
    where each rule is represented by a list of spans (can be an empty list if no spans):
    [span_1, span_2, ..., span_N]

    So we have: [[[span_1_rule_1_doc_1, span_2_rule_1_doc_1], [span_1_rule_2_doc_1]]]

    :param df:
    :param rules:
    :return:
    """
    entity_fns = []
    for rule in rules:
        if rule.rule_type == "noun_phrase_regex":
            sentence, text_regex, noun_phrase_regex, entity_id = parse_ner_noun_phrase_regex_rule(rule)
            entity_fns.append(create_noun_phrase_regex_lf(sentence,
                                                          text_regex,
                                                          noun_phrase_regex,
                                                          entity_id))
        elif rule.rule_type == "entity_regex":
            regex, match_num_entitities, entity_id = parse_ner_regex_rule(rule)
            entity_fns.append(create_regex_span_lf(regex,
                                                   match_num_entitities,
                                                   entity_id))
        else:
            raise Exception(f"Rule type {rule.rule_type} is not supported for named-entity-recognition projects.")
    all_entity_spans = apply_rules_ner(df, entity_fns)
    return all_entity_spans


def apply_update_rules_ner(project, es_uri, df, new_rules):
    """
    Get all the current spans, apply the new rules, get predicted label and rule stats (but not accuracy).

    Save stats and new rule in db, new rule labels in ES.

    :param project:
    :param df:
    :param rules:
    :return:
    """
    new_rule_ids = [rule.id for rule in new_rules]
    old_rule_ids = [rule.id for rule in project.rules if rule.id not in new_rule_ids]
    all_rule_ids = [rule.id for rule in project.rules]

    # 1. get all the spans from ES
    rule_spans, predicted_label_spans = ner_es.get_all_existing_rule_spans(es_uri,
                                                                           project.index_name,
                                                                           old_rule_ids,
                                                                           project.total_documents)

    # 2. compute new spans
    new_rule_spans = get_new_spans(df, new_rules)

    # 3. get predicted_label
    merged_predicted_label_spans = nlp_ner.merge_predicted_labels(predicted_label_spans, new_rule_spans)

    # 4. compute rule stats (except accuracy) for all rules + predicted_label
    all_rule_spans = [old + new for old, new in zip(rule_spans, new_rule_spans)]
    stats = ner_metrics.get_rule_entity_stats(all_rule_ids, all_rule_spans, merged_predicted_label_spans)

    # 5. save stats to db
    # for each rule, save: total overlaps, coverage (total documents)
    db.update_rules_project_ner(project, stats)

    # 6. save new rule spans and predicted_label spans to ES
    index_spans(es_uri,
                project.index_name,
                merged_predicted_label_spans,
                all_rule_spans,
                all_rule_ids)
    return


def apply_rules(project, es_uri, new_rules):
    """
    Applies all the rules, recomputes merged label file and returns summary stats
    """
    df = read_data_df(project.data_filepath,
                      spacy_binary_filepath=project.spacy_binary_filepath)

    if project.type == PROJECT_TYPE_CLASSIFICATION:
        apply_update_rules_classification(project, es_uri, df, new_rules)
    elif project.type == PROJECT_TYPE_NER:
        apply_update_rules_ner(project, es_uri, df, new_rules)


def add_rule(session,
             project,
             es_uri,
             rule_type,
             rule_name,
             params,
             class_id,
             class_name,
             create_rule_id):
    project, new_rule = db_ops.add_rule(project,
                                        rule_type,
                                        rule_name,
                                        params,
                                        class_id,
                                        class_name,
                                        create_rule_id)
    session.flush()  # need to flush to get the rule ids
    # apply rule and save labels to file & index to ES
    apply_rules(project, es_uri, [new_rule])


def add_rules(session, project, es_uri, rule_file_bytes, import_id):
    file = utils.get_decoded_stream(rule_file_bytes)
    rules = csv.DictReader(file)
    project, rules = db_ops.add_rules(project, rules)
    session.flush()
    apply_rules(project, es_uri, rules)
    project.import_id = import_id
    return import_id


def compute_rule_accuracy(rule_ids,
                          all_labels_mat,
                          merged_labels_mat,
                          manual_labels,
                          entity_ids):
    accuracy_dict = metrics.get_rule_accuracy_from_mats(all_labels_mat,
                                                        manual_labels,
                                                        rule_ids)

    entity_metrics, global_metrics = metrics.get_merged_accuracy_from_mats(merged_labels_mat,
                                                                           manual_labels,
                                                                           entity_ids,
                                                                           compute_precision_recall=True)

    stats = {}

    for rule_id in rule_ids:
        stats[rule_id] = {'accuracy': accuracy_dict[rule_id]}

    stats["merged"] = entity_metrics
    stats["merged_all"] = global_metrics

    return stats


def get_sentiment_distribution(filepath, spacy_binary_filepath):
    df = read_data_df(filepath, spacy_binary_filepath, only_sentiment=True)
    distribution = ml_sentiment.get_sentiment_distribution(df)
    return distribution


def get_ground_truth_mats(entity_ids, es_uri, index_name):
    docs = classification_es.get_all_ground_truth_labels(es_uri, index_name)
    ground_truth_labels_mat, all_merged_labels_mat = get_ground_truth_mats_from_es_docs(entity_ids, docs)
    return ground_truth_labels_mat, all_merged_labels_mat


def get_ground_truth_accuracy_stats_classification(entity_ids, es_uri, index_name):
    ground_truth_labels_mat, all_merged_labels_mat = get_ground_truth_mats(entity_ids, es_uri, index_name)
    entity_metrics = get_ground_truth_distribution_stats(entity_ids,
                                                         ground_truth_labels_mat,
                                                         all_merged_labels_mat)

    return entity_metrics


def get_rule_accuracy_stats_classification(project,
                                           rule_ids,
                                           all_labels_mat,
                                           merged_labels,
                                           manual_labels):
    entity_ids = [supervised_class.id for supervised_class in project.classes]

    stats = compute_rule_accuracy(rule_ids,
                                  all_labels_mat,
                                  merged_labels,
                                  manual_labels,
                                  entity_ids)

    return stats


def check_create_rule_id(session, create_rule_id):
    rule = db_ops.get_rule_by_create_rule_id(session, create_rule_id)
    if rule:
        return rule.create_rule_id
    return None


def read_data_df(data_filepath, spacy_binary_filepath, only_sentiment=False):
    with open(data_filepath, 'r') as file:
        column_names = get_column_names(file)
        usecols = list(sentiment.SENTIMENT_COL_MAPPING.values())
        if ES_GROUND_TRUTH_NAME_FIELD in column_names:
            usecols.append(ES_GROUND_TRUTH_NAME_FIELD)
        df = pd.read_csv(file, encoding='utf8', usecols=usecols)
        if not only_sentiment:
            spacy_docs = spacy_file_utils.deserialise_spacy_docs(spacy_binary_filepath)
            df[SPACY_COLUMN_NAME] = spacy_docs
    return df
