import unittest

from knowledge_hub import source_reader


PROBLEM = "Exact membership tests are expensive for dense microbial archives."
METHOD = "RiverNet compresses the archive into a Bloom filter and verifies only surviving candidates."
RESULT = "On CoralBench, RiverNet increases recall from 71% to 83% against HashTable."
COST = "RiverNet uses 240 MB of peak RAM and takes 18 ms per query on a single CPU."
LIMIT = "Our evaluation is limited to static archives and does not cover online updates."
BASELINE = "HashTable resolves archive membership exactly by comparing the keys in the matching bucket."


def source(source_id, item_id, version_id, title, quotes, scope="full"):
    source_url = f"https://example.org/paper/{item_id}v{version_id}"
    return {
        "id": source_id, "item_id": item_id, "version_id": version_id,
        "title": title, "scope": scope, "source_url": source_url, "label": f"v{version_id}",
        "excerpts": [
            {
                "anchor": f"p{index}", "source_url": source_url, "quality": "paragraph",
                "reading_url": f"/items/{item_id}?version={version_id}#p{index}", "quote": quote,
            }
            for index, quote in enumerate(quotes, 1)
        ],
    }


class SourceReaderTests(unittest.TestCase):
    def setUp(self):
        self.focal = source("S1", 8, "12", "RiverNet", [
            PROBLEM + " " + METHOD, RESULT, COST, LIMIT,
        ])
        self.background = source("S2", 5, "7", "HashTable for membership tests", [BASELINE])
        self.context = {"sources": [self.focal, self.background],
                        "saved_learning": [{"understanding": "private sentinel"}]}

    def selected(self, question, *, parent=None):
        answer = source_reader.ask(self.context, question, parent)
        return [c for s in answer["sections"] for c in s["citations"]]

    def test_chinese_facet_questions_select_different_english_sentences(self):
        cases = [
            ("這篇解決什麼問題？", PROBLEM),
            ("作者的方法是什麼？", METHOD),
            ("這個實驗的量化結果如何？", RESULT),
            ("推論成本多少？", COST),
            ("作者提到哪些限制？", LIMIT),
        ]
        for question, quote in cases:
            with self.subTest(question=question):
                cited = self.selected(question)[0]
                self.assertIn(quote, cited["quote"])
                self.assertEqual((cited["source_id"], cited["version_id"]), ("S1", "12"))
                self.assertIn("version=12#", cited["reading_url"])

    def test_free_query_requires_terms_and_the_requested_facet(self):
        for question, expected in (
            ("Bloom filter 怎麼用？", METHOD),
            ("CoralBench 的 recall 提升多少？", RESULT),
            ("哪些方法有採用 Bloom filter？", METHOD),
        ):
            with self.subTest(question=question):
                self.assertIn(expected, self.selected(question)[0]["quote"])
        for question in ("用 Bloom filter 有多少消融實驗的結果？", "CoralBench 的 precision 提升多少？",
                         "台北的租屋成本多少？", "What is the price of train tickets?",
                         "RiverNet 有用 KDTree 嗎？", "Bloom filter 會造成記憶洩漏嗎？"):
            with self.subTest(question=question):
                result = source_reader.ask(self.context, question)
                self.assertEqual(result["sections"], [])
                self.assertTrue(result["limitations"])

    def test_comparison_needs_two_relevant_fixed_versions(self):
        sections = source_reader.ask(self.context, "和 HashTable 有什麼差別？")["sections"]
        compare = next(section for section in sections if section["role"] == "comparison")
        self.assertEqual({(c["source_id"], c["version_id"]) for c in compare["citations"]},
                         {("S1", "12"), ("S2", "7")})
        self.assertIn(BASELINE, compare["citations"][1]["quote"])
        self.assertEqual(self.selected("和 KDTree 有什麼不同？"), [])

    def test_answer_body_identifies_operations_and_measured_conditions(self):
        method = source_reader.ask(self.context, "作者的方法是什麼？")["sections"][0]["text"]
        self.assertIn("compresses the archive into a Bloom filter", method)
        self.assertIn("verifies only surviving candidates", method)
        result = source_reader.ask(self.context, "CoralBench 的 recall 提升多少？")["sections"][0]["text"]
        self.assertIn("recall from 71% to 83%", result)
        self.assertIn("CoralBench", result)
        self.assertIn("HashTable", result)
        cost = source_reader.ask(self.context, "成本多少？")["sections"][0]["text"]
        self.assertIn("240 MB of peak RAM", cost)
        self.assertIn("18 ms per query", cost)
        self.assertIn("single CPU", cost)

    def test_comparison_about_cost_uses_cost_evidence_from_both_sides(self):
        self.focal["excerpts"][2]["quote"] = (
            "On a single CPU, RiverNet reduces per-query latency from 40 ms to 18 ms against HashTable."
        )
        self.background["excerpts"].append({
            **self.background["excerpts"][0],
            "anchor": "p2",
            "quote": "On a single CPU, HashTable measures a latency of 40 ms per query.",
        })
        answer = source_reader.ask(self.context, "跟 HashTable 相比，延遲降低多少？")
        comparison = answer["sections"][0]
        self.assertEqual(comparison["role"], "comparison")
        self.assertIn("latency from 40 ms to 18 ms", comparison["text"])
        self.assertEqual([c["anchor"] for c in comparison["citations"]], ["p3", "p2"])
        self.background["excerpts"].pop()
        brief = source_reader.ask(self.context, "跟 HashTable 相比，延遲降低多少？")
        self.assertEqual(brief["sections"][0]["role"], "answer")
        self.assertIn("原文", brief["limitations"])

    def test_accepts_spelled_out_latency_units_and_plain_retrieval_terms(self):
        self.focal["excerpts"][2]["quote"] = (
            "Our retrieval pipeline reduces search latency from 80 milliseconds to 26 milliseconds "
            "on the ImageGallery benchmark."
        )
        question = "ImageGallery 的檢索延遲是多少？"
        quote = self.selected(question)[0]["quote"]
        self.assertIn("from 80 milliseconds to 26 milliseconds", quote)
        retrieval = source_reader.retrieval_query(question)
        self.assertIn("retriev", retrieval)
        self.assertNotIn(r"\b", retrieval)
        self.assertNotIn("|", retrieval)

    def test_does_not_substitute_a_different_cost_measure(self):
        self.focal["excerpts"][2]["quote"] = "On a single CPU, RiverNet needs 240 MB of peak RAM."
        answer = source_reader.ask(self.context, "延遲降低多少？")
        self.assertEqual(answer["sections"], [])
        answer = source_reader.ask(self.context, "更省資源嗎？")
        self.assertEqual(answer["sections"], [])

    def test_enumerates_only_explicitly_measured_not_evaluation_setup(self):
        self.focal["excerpts"][1]["quote"] = "We measured recall on 3 datasets with 100 examples each."
        answer = source_reader.ask(self.context, "這篇的 recall 提升多少？")
        self.assertEqual(answer["sections"], [])
        evidence = self.selected("用什麼資料集評估？")[0]
        self.assertIn("3 datasets", evidence["quote"])

    def test_pronoun_reuses_parent_topic_with_new_facet(self):
        parent = {"prompt": "Bloom filter 怎麼用？"}
        self.focal["excerpts"][2]["quote"] += " The Bloom filter uses 80 MB of RAM."
        quote = self.selected("那它的成本呢？", parent=parent)[0]["quote"]
        self.assertIn("The Bloom filter uses 80 MB of RAM.", quote)
        self.assertIn("bloom", source_reader.retrieval_query("那它的成本呢？", parent).casefold())
        self.assertIn("latency", source_reader.retrieval_query("成本多少？").casefold())
        self.assertIn(RESULT, self.selected("CoralBench 的 recall 提升多少？", parent=parent)[0]["quote"])

    def test_lesson_has_cited_mechanism_check_and_two_sided_comparison(self):
        result = source_reader.lesson(self.context)
        by_role = {section["role"]: section for section in result["sections"]}
        self.assertTrue({"problem", "mechanism", "comparison", "check"} <= by_role.keys())
        self.assertIn(METHOD, by_role["mechanism"]["citations"][0]["quote"])
        self.assertNotIn("private sentinel", str(result))
        for section in result["sections"]:
            for cite in section["citations"]:
                original = next(s for s in self.context["sources"] if s["id"] == cite["source_id"])
                span = next(e for e in original["excerpts"] if e["anchor"] == cite["anchor"])
                self.assertIn(cite["quote"], span["quote"])
                self.assertEqual(cite["source_url"], span["source_url"])

    def test_abstract_does_not_fill_missing_results_or_context(self):
        abstract = source("S1", 8, "12", "RiverNet", [METHOD], scope="abstract")
        result = source_reader.lesson({"sources": [abstract]})
        self.assertIn("摘要", result["limitations"])
        self.assertFalse(any(s["role"] == "comparison" for s in result["sections"]))
        answer = source_reader.ask({"sources": [abstract]}, "有哪些量化結果？")
        self.assertEqual(answer["sections"], [])
        self.assertIn("摘要", answer["limitations"])

    def test_refuses_misaligned_columns_and_citation_lists(self):
        self.focal["excerpts"] = [
            {"anchor": "s", "quote": "RiverNet reaches      accuracy and recall at 93% according to the experiment.",
             "quality": "columns", "source_url": "", "reading_url": ""},
        ]
        self.assertEqual(self.selected("實驗結果如何？"), [])


class NaturalLanguageEvidenceTests(unittest.TestCase):
    def answer(self, quotes, prompt, parent=None, backgrounds=()):
        article = source("S1", 6, "3", "QuillArchive", quotes)
        return source_reader.ask({"sources": [article, *backgrounds]}, prompt, parent)

    def test_dataset_accuracy_and_strongest_baseline_gap_are_paired(self):
        correct = (
            "On LanternBench, exact-answer accuracy reaches 82.6%, "
            "where it leads the strongest baseline by 3.4 points."
        )
        answer = self.answer([
            "On LanternBench, recall reaches 92.7% and leads the strongest baseline by 9.8 points.",
            "On MeadowSuite, accuracy reaches 91.3%, with a 6.4 point gain against the strongest baseline.",
            correct,
        ], "資料集 LanternBench 的準確率是多少，比最強基線好幾個百分點？")
        self.assertEqual([c["quote"] for c in answer["sections"][0]["citations"]], [correct])
        self.assertIn("準確率", answer["sections"][0]["text"])
        self.assertIn("82.6%", answer["sections"][0]["text"])
        self.assertIn("3.4 個百分點", answer["sections"][0]["text"])
        self.assertNotIn("92.7%", answer["sections"][0]["text"])

    def test_adjacent_sentence_can_establish_an_otherwise_unlabeled_metric(self):
        context = "We measure only answer accuracy on LanternBench."
        result = "QuillArchive reaches 77.4% on LanternBench, where it leads the strongest baseline by 1.8 points."
        prompt = "LanternBench 的準確率是多少，比最強基線好幾個百分點？"
        answer = self.answer([context + " " + result], prompt)
        quotes = [c["quote"] for c in answer["sections"][0]["citations"]]
        self.assertIn(context, " ".join(quotes))
        self.assertIn(result, " ".join(quotes))
        self.assertIn("77.4%", answer["sections"][0]["text"])
        self.assertEqual(self.answer([result], prompt)["sections"], [])

    def test_dataset_and_gap_cannot_be_borrowed_from_another_measure(self):
        prompt = "LanternBench 的準確率是多少，比最強基線好幾個百分點？"
        for quote in [
            "Accuracy reaches 81.5% on MeadowSuite, while recall on LanternBench leads the strongest baseline by 9 points.",
            "On LanternBench, accuracy reaches 81.5%, while recall reaches 94% and leads the strongest baseline by 9 points.",
            "On LanternBench, accuracy reaches 81.5%, where it leads a random baseline by 9 points.",
            "On LanternBench, precision reaches 82.2%, where it leads the strongest baseline by 3 points.",
        ]:
            with self.subTest(quote=quote):
                self.assertEqual(self.answer([quote], prompt)["sections"], [])

    def test_explicit_no_rewrite_answers_record_question_and_pronoun(self):
        keeper = "LeafBundler merges duplicate tags in the index without rewriting any original record."
        same_actor_before = "LeafBundler reaches 89% accuracy on MeadowSuite in the single-node setting."
        unrelated = "SlatePacker preserves original records when formatting the archived results."
        for prompt, parent in [
            ("元件 LeafBundler 會動到原本的紀錄嗎？", None),
            ("那它會改動既有記錄嗎？", {"prompt": "LeafBundler 在 MeadowSuite 的準確率是多少？"}),
        ]:
            with self.subTest(prompt=prompt):
                sections = self.answer([unrelated, same_actor_before, keeper], prompt, parent)["sections"]
                self.assertEqual([c["quote"] for c in sections[0]["citations"]], [keeper])
                self.assertIn("既有紀錄不會被重寫", sections[0]["text"])
                self.assertIn("合併", sections[0]["text"])

    def test_record_mutation_requires_the_right_actor_and_operation(self):
        question = "LeafBundler 會覆寫原本的紀錄嗎？"
        unrelated = "SlatePacker does not rewrite any original records during consolidation."
        creation = "LeafBundler writes a new audit record whenever each batch is merged."
        only_index = "LeafBundler changes the index without rewriting the index configuration."
        self.assertEqual(self.answer([unrelated, creation, only_index], question)["sections"], [])
        overwrite = "LeafBundler overwrites existing records during nightly compaction only."
        sections = self.answer([overwrite], question)["sections"]
        self.assertIn("會覆寫既有紀錄", sections[0]["text"])
        self.assertIn("nightly compaction only", sections[0]["citations"][0]["quote"])

    def test_each_side_names_its_own_cost_reduction_step_and_condition(self):
        index = "FernIndex lowers retrieval cost by pruning whole shards during routing on the sparse collection."
        search = "PebbleScan reduces search work by caching candidates before reranking on the dense collection."
        unrelated = "PebbleScan takes 51 ms per query on a single CPU."
        for question in [
            "模型 FernIndex 和 PebbleScan 各在哪一步減少檢索成本？",
            "Where do FernIndex and PebbleScan each reduce retrieval cost?",
        ]:
            with self.subTest(question=question):
                sections = self.answer([index, unrelated, search], question)["sections"]
                self.assertEqual(sections[0]["role"], "comparison")
                self.assertEqual([c["quote"] for c in sections[0]["citations"]], [index, search])
                for phrase in ("pruning whole shards", "before reranking", "sparse collection", "dense collection"):
                    self.assertIn(phrase, sections[0]["text"])
                self.assertEqual(self.answer([index, unrelated], question)["sections"], [])

    def test_each_named_method_can_use_a_separate_fixed_background(self):
        one = "FernIndex reduces search cost by dropping empty shards during routing for sparse queries."
        two = "PebbleScan reduces retrieval work by memoizing candidates before reranking on the cached replay."
        background = source("S3", 17, "9", "PebbleScan at scale", [two])
        question = "FernIndex 與 PebbleScan 分別在哪個步驟降低檢索成本？"
        result = self.answer([one], question, backgrounds=[background])
        self.assertEqual([(c["source_id"], c["version_id"]) for c in result["sections"][0]["citations"]],
                         [("S1", "3"), ("S3", "9")])

    def test_lesson_prefers_explicit_opening_problem_and_contribution(self):
        main_problem = "Our central challenge is that coastal archives cannot associate seasonal records with the right locations."
        main_method = "We introduce QuillArchive, an approach that links uncertain records to overlapping geographic windows."
        peripheral_problem = "The diagnostic wrapper fails on an expensive trace and requires significant logging when its parser runs."
        peripheral_method = "The fallback wrapper processes, caches, verifies, indexes, and ranks the rendered debug payload."
        article = source("S1", 6, "3", "QuillArchive", [
            peripheral_problem + " " + peripheral_method, main_problem + " " + main_method,
            "It states the problem and the three formal requirements, then introduces the writer and retriever.",
        ])
        article["excerpts"][0]["anchor"] = "Appendix.D"
        article["excerpts"][1]["anchor"] = "abstract"
        result = source_reader.lesson({"sources": [article]})
        sections = {section["role"]: section for section in result["sections"]}
        self.assertEqual(sections["problem"]["citations"][0]["quote"], main_problem)
        self.assertEqual(sections["mechanism"]["citations"][0]["quote"], main_method)
        self.assertIn("核心問題", sections["problem"]["text"])
        self.assertIn("提出", sections["mechanism"]["text"])

    def test_evaluation_protocol_can_be_stated_as_judging_and_voting(self):
        protocol = "MapleTrial and FinchScale use an OpalJudge evaluator with majority-of-5 voting."
        for question in ("MapleTrial 的評估方式是什麼？", "MapleTrial 怎麼打分？", "How is MapleTrial scored?"):
            with self.subTest(question=question):
                sections = self.answer([
                    "MapleTrial has the highest accuracy across the joint benchmarks, at 72.9%, ahead of both baselines.",
                    protocol,
                ], question)["sections"]
                self.assertEqual(sections[0]["citations"][0]["quote"], protocol)
                self.assertIn("評分", sections[0]["text"])
                self.assertIn("5", sections[0]["text"])

    def test_related_work_does_not_supply_the_focal_mechanism_or_comparison_side(self):
        earlier = "Earlier methods by TimberHash index, cache, rank, and merge requests to preserve recall."
        contribution = "We introduce QuillArchive, which hashes the request and verifies the associated record."
        baseline = "TimberHash indexes each request by matching keys in a local hash table."
        article = source("S1", 6, "3", "QuillArchive", [earlier, contribution])
        article["excerpts"][0]["section_role"] = "related"
        article["excerpts"][1]["section_role"] = "abstract"
        background = source("S2", 9, "1", "TimberHash", [baseline])
        result = source_reader.lesson({"sources": [article, background]})
        sections = {section["role"]: section for section in result["sections"]}
        self.assertEqual(sections["mechanism"]["citations"][0]["quote"], contribution)
        if "comparison" in sections:
            self.assertEqual(sections["comparison"]["citations"][0]["quote"], contribution)

    def test_multiple_reported_benchmarks_keep_the_gap_with_its_own_percentage(self):
        quote = (
            "QuillArchive has the highest accuracy on both benchmarks, 87.2% on LanternBench "
            "and 71.9% on MeadowSuite, 11.5 points above the strongest baseline."
        )
        answer = self.answer([quote], "MeadowSuite 的準確率是多少，比最強基線好幾個百分點？")
        self.assertTrue(answer["sections"])
        explanation = answer["sections"][0]["text"].split("原文「")[0]
        self.assertIn("MeadowSuite", explanation)
        self.assertIn("71.9%", explanation)
        self.assertIn("11.5 個百分點", explanation)
        self.assertNotIn("87.2%", explanation)
        wrong = self.answer([quote], "LanternBench 的準確率是多少，比最強基線好幾個百分點？")
        self.assertEqual(wrong["sections"], [])

    def test_record_mention_in_a_reference_cannot_adopt_another_actors_decision(self):
        other_subject = (
            "Unlike LeafBundler, SlatePacker consolidates duplicate tags without rewriting any original records."
        )
        result = self.answer([other_subject], "LeafBundler 會覆寫原本的紀錄嗎？")
        self.assertEqual(result["sections"], [])


    def test_plain_accuracy_question_prefers_headline_result_over_ablation(self):
        ablation = ("On LanternBench, the index raises accuracy from 77.1% to 84.2% "
                    "under one low-memory setting.")
        summary = ("QuillArchive has the highest accuracy on both benchmarks, 88.1% on LanternBench "
                   "and 71.9% on MeadowSuite, 11.5 points above the strongest baseline.")
        sections = self.answer([ablation, summary], "LanternBench 的準確率是多少？")["sections"]
        self.assertEqual(sections[0]["citations"][0]["quote"], summary)
        explanation = sections[0]["text"].split("原文「")[0]
        self.assertIn("88.1%", explanation)
        self.assertNotIn("11.5", explanation)

    def test_vague_contribution_can_be_expanded_by_an_adjacent_mechanism(self):
        headline = "We present QuillArchive, a new approach to improve decisions across cultural archives."
        detail = "The procedure indexes overlapping regions and verifies each candidate against its original record."
        caption_tail = ("An unrelated caption caches, verifies, filters and indexes more conditions "
                        "than the actual approach.")
        article = source("S1", 6, "3", "QuillArchive", [
            headline + " " + detail + " Gray circles and chart marks Figure 3 Layout summary. " + caption_tail,
        ])
        article["excerpts"][0]["section_role"] = "abstract"
        sections = source_reader.lesson({"sources": [article]})["sections"]
        mechanism = next(section for section in sections if section["role"] == "mechanism")
        self.assertEqual([c["quote"] for c in mechanism["citations"]], [headline, detail])
        self.assertIn("索引", mechanism["text"])


    def test_english_claim_compares_the_same_benchmark_to_the_strongest_baseline(self):
        quote = ("On MapleTrial, QuillArchive obtains an accuracy of 80.2%, "
                 "leading the strongest baseline by 6.2 percentage points.")
        answer = self.answer([quote], "What is MapleTrial's accuracy, and how many points above the strongest baseline?")
        self.assertTrue(answer["sections"])
        self.assertIn("80.2%", answer["sections"][0]["text"])
        self.assertIn("6.2 個百分點", answer["sections"][0]["text"])

    def test_result_of_other_benchmark_cannot_answer_a_training_dataset_mention(self):
        quote = "After training on MeadowSuite, QuillArchive reaches an accuracy of 87.2% on LanternBench."
        answer = self.answer([quote], "MeadowSuite 的準確率是多少？")
        self.assertEqual(answer["sections"], [])

    def test_comparison_and_self_check_use_the_concrete_mechanism(self):
        headline = "We present QuillArchive, a practical and accurate archive index."
        detail = "QuillArchive indexes overlapping archive regions and verifies candidates against original records."
        article = source("S1", 6, "3", "QuillArchive", [headline + " " + detail])
        article["excerpts"][0]["section_role"] = "abstract"
        other = source("S2", 2, "4", "DuneMatch",
                       ["DuneMatch indexes archive regions by matching the text in query records."])
        sections = {section["role"]: section for section in
                    source_reader.lesson({"sources": [article, other]})["sections"]}
        self.assertEqual(sections["comparison"]["citations"][0]["quote"], detail)
        self.assertEqual(sections["check"]["citations"][0]["quote"], detail)
        self.assertIn("索引", sections["check"]["text"])
        self.assertNotIn("We present", sections["check"]["text"])

    def test_self_check_asks_about_full_method_when_only_announcement_is_available(self):
        headline = "We present QuillArchive, a practical approach for studying cultural archives."
        article = source("S1", 6, "3", "QuillArchive", [headline])
        sections = {section["role"]: section for section in
                    source_reader.lesson({"sources": [article]})["sections"]}
        self.assertNotIn("We present", sections["check"]["text"])
        self.assertIn("方法", sections["check"]["text"])

    def test_lesson_separates_setup_and_resource_measurements_from_desired_outcomes(self):
        quotes = [
            "QuillArchive reaches an accuracy of 81.2% on all selected benchmarks.",
            "Across four datasets, QuillArchive reaches a higher accuracy of 82.7%.",
            "Benchmarks now measure this ability on geographic, textual, and network archives.",
            "To remain usable, QuillArchive needs efficient retrieval and lower latency across 4 benchmarks.",
        ]
        article = source("S1", 6, "3", "QuillArchive", quotes)
        sections = {section["title"]: section for section in
                    source_reader.lesson({"sources": [article]})["sections"]}
        self.assertNotIn("實驗是怎麼做的", sections)
        self.assertNotIn("原文中的資源用量", sections)
        protocol = "We evaluate QuillArchive on two geographically disjoint archives using exact-match scoring."
        cost = "QuillArchive takes 14 ms per query on a single CPU when the index fits in memory."
        article = source("S1", 6, "3", "QuillArchive", quotes + [protocol, cost])
        sections = {section["title"]: section for section in
                    source_reader.lesson({"sources": [article]})["sections"]}
        self.assertEqual(sections["實驗是怎麼做的"]["citations"][0]["quote"], protocol)
        self.assertEqual(sections["原文中的資源用量"]["citations"][0]["quote"], cost)


    def test_cost_setup_can_be_labeled_as_measurement_conditions_without_a_numeric_claim(self):
        protocol = "Efficiency is measured as ingestion time and answer latency, with caching disabled in every timed run."
        article = source("S1", 6, "3", "QuillArchive", [protocol])
        article["excerpts"][0]["section_role"] = "results"
        sections = {section["title"]: section for section in
                    source_reader.lesson({"sources": [article]})["sections"]}
        self.assertNotIn("原文中的資源用量", sections)
        self.assertEqual(sections["資源怎麼測量"]["citations"][0]["quote"], protocol)
