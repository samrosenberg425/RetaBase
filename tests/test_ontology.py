"""Behavioral boundaries for ontology v1; offline, no biomedical network calls."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retarats_pipeline.curation.ontology import annotate, evidence_scope, synthesis_methods, validate
from retarats_pipeline.curation.facets import derive_facets
from retarats_pipeline.curation.reliability import assess_reliability


class OntologyTests(unittest.TestCase):
    def row(self, title='', abstract='', **extra):
        return dict(evidence_id='pilot:test', title=title, abstract=abstract, **extra)

    def test_dictionary(self):
        self.assertEqual(validate(), [])

    def test_background_is_not_studied_condition(self):
        r = self.row(abstract='BACKGROUND: Obesity and type 2 diabetes are common. METHODS: We enrolled healthy volunteers.')
        self.assertEqual(annotate(r)[0]['facet_condition_studied'], '')

    def test_exclusion_is_not_studied_condition(self):
        r = self.row(abstract='METHODS: Patients with type 2 diabetes were excluded.')
        self.assertEqual(annotate(r)[0]['facet_condition_studied'], '')

    def test_population_and_outcome_remain_distinct(self):
        r = self.row('A trial in adults with obesity', 'METHODS: HbA1c was measured at 24 weeks.')
        o, _ = annotate(r)
        self.assertEqual(o['facet_condition_studied'], 'obesity')
        self.assertEqual(o['facet_outcome_measured'], 'hba1c')
        self.assertNotIn('type_2_diabetes', o['facet_condition_studied'])

    def test_potential_indication_not_promoted(self):
        r = self.row(abstract='This study examined plant metabolites for their potential to address insulin resistance in type 2 diabetes.')
        self.assertEqual(annotate(r)[0]['facet_condition_studied'], '')

    def test_symptom_weight_loss_not_body_weight_measurement(self):
        r = self.row(abstract='We present a case of a man with decreased appetite and weight loss.')
        self.assertEqual(annotate(r)[0]['facet_outcome_measured'], '')

    def test_not_measured_not_assigned(self):
        r = self.row(abstract='METHODS: HbA1c was not measured.')
        self.assertEqual(annotate(r)[0]['facet_outcome_measured'], '')

    def test_enzymes_not_histology(self):
        r = self.row(abstract='METHODS: ALT was measured after treatment.')
        self.assertEqual(annotate(r)[0]['facet_outcome_measured'], 'liver_enzymes')
        self.assertNotIn('liver_histology', derive_facets(r, r).wide['facet_endpoint'])

    def test_human_cell_lines_are_not_clinical(self):
        r = self.row('Drug activity in human cell lines', model_type='human', primary_study_type='Human observational')
        self.assertEqual(evidence_scope(r), 'nonclinical')
        self.assertEqual(assess_reliability(r).evidence_class, 'in_vitro')
        self.assertEqual(annotate(r)[0]['facet_experimental_system'], 'cell_line')
        self.assertNotIn('cell_line', derive_facets(r, r).wide['facet_species'])

    def test_review_is_not_model_system(self):
        r = self.row(model_type='review')
        self.assertNotIn('review', derive_facets(r, r).wide['facet_model_system'])

    def test_synthesis_methods_are_compatible(self):
        r = self.row('Systematic review and meta-analysis of clinical trials in patients')
        self.assertEqual(synthesis_methods(r), ['systematic_review', 'meta_analysis'])
        self.assertEqual(assess_reliability(r).evidence_level_key, 'systematic_review')

    def test_slash_category_does_not_assert_both_methods(self):
        r = self.row('An overview', primary_study_type='Systematic review / Meta-analysis')
        self.assertEqual(synthesis_methods(r), [])
        self.assertIn('legacy_synthesis_method_unconfirmed', annotate(r)[0]['ontology_review_reason'])

    def test_animal_review_not_top_human_evidence(self):
        r = self.row('Systematic review and meta-analysis of mouse studies')
        rel = assess_reliability(r)
        self.assertEqual(rel.evidence_level_key, 'synthesis_nonclinical')
        self.assertLess(rel.evidence_directness, 55)

    def test_unknown_review_not_assumed_human(self):
        r = self.row('A systematic review of treatment effects')
        self.assertEqual(assess_reliability(r).evidence_level_key, 'synthesis_unknown')

    def test_background_patients_do_not_change_animal_review(self):
        r = self.row('Systematic review of preclinical studies', 'BACKGROUND: Patients need better treatment. METHODS: We included animal studies.')
        self.assertEqual(evidence_scope(r), 'nonclinical')

    def test_mixed_review(self):
        r = self.row('Systematic review', 'METHODS: We included clinical studies and in vitro studies.')
        self.assertEqual(evidence_scope(r), 'mixed')
        self.assertEqual(assess_reliability(r).evidence_level_key, 'synthesis_mixed')

    def test_mixed_primary_report(self):
        r = self.row('Drug treatment in adults and mice', primary_study_type='RCT', model_type='human')
        self.assertEqual(evidence_scope(r), 'mixed')
        self.assertEqual(assess_reliability(r).evidence_level_key, 'mixed_evidence')

    def test_explicit_nonrandomized_overrides_legacy_rct(self):
        r = self.row('Mouthwash: a non-randomized study', primary_study_type='RCT', model_type='human')
        rel = assess_reliability(r)
        self.assertEqual(rel.evidence_class, 'human_clinical')
        self.assertEqual(rel.evidence_level_key, 'nonrandomized_trial')

    def test_narrative_topics_not_studied_conditions(self):
        r = self.row('Adipose tissue in type 2 diabetes', primary_study_type='Review / narrative')
        self.assertEqual(annotate(r)[0]['facet_condition_studied'], '')

    def test_traceable_and_deterministic(self):
        r = self.row('Treatment in adults with obesity')
        o, annotations = annotate(r)
        self.assertEqual(annotate(r), (o, annotations))
        self.assertTrue(annotations)
        self.assertEqual(len({a['annotation_id'] for a in annotations}), len(annotations))
        self.assertTrue(all(a['source_text'] == r['title'] and a['review_status'] == 'machine_unreviewed' for a in annotations))
        self.assertEqual(json.loads(o['ontology_annotations']), annotations)


if __name__ == '__main__':
    unittest.main()
