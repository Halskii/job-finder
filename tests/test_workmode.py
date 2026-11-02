"""Reading in person, hybrid or remote from what a posting says."""
import unittest

import workmode


class DetectTests(unittest.TestCase):
    def check(self, expected, title="Software Engineer", location="Austin, TX", description=""):
        self.assertEqual(workmode.detect(title, location, description), expected, (title, location, (description or "")[:80]))

    def test_the_location_line_and_the_title_count(self):
        self.check(["remote"], location="Remote - US")
        self.check(["remote"], location="San Francisco, CA | Remote")
        self.check(["hybrid"], title="Software Engineer (Hybrid)")
        self.check(["hybrid"], location="Hybrid - Austin, TX")
        self.check(["remote"], title="Backend Engineer, Work From Home")
        self.check(["onsite"], location="Austin, TX (On-site)")

    def test_how_a_posting_states_it(self):
        self.check(["remote"], description="This is a fully remote position open to candidates across the US.")
        self.check(["remote"], description="We are remote-first and hire people anywhere in the country.")
        self.check(["remote"], description="Remote work is available for this role.")
        self.check(["remote"], description="Work from home with a company laptop.")
        self.check(["remote"], description="Work arrangement: Remote")
        self.check(["hybrid"], description="We offer a hybrid work schedule.")
        self.check(["hybrid"], description="You will be in the office three days a week.")
        self.check(["hybrid"], description="Expect 2 days per week in the office.")
        self.check(["hybrid"], description="Telework: Hybrid")
        self.check(["hybrid"], description="**Telework:**\nHybrid")
        self.check(["onsite"], description="This role is on-site in our Austin headquarters.")
        self.check(["onsite"], description="This position is not remote.")
        self.check(["onsite"], description="Telework: In Office")
        self.check(["onsite"], description="Must be able to work in person at our facility.")

    def test_a_posting_that_does_not_say_gives_nothing(self):
        self.check([], description="Build services in Python and Go. We value curiosity.")
        self.check([], description="")
        self.check([], title="", location="", description=None)

    def test_words_that_only_mention_the_idea_do_not_count(self):
        self.check([], title="Cloud Engineer", description="Experience with hybrid cloud and hybrid infrastructure.")
        self.check([], description="Benefits include on-site child care, an on-site gym and remote-friendly perks.")
        self.check([], description="Our onsite cafeteria serves lunch.")

    def test_a_title_or_location_that_names_the_arrangement_is_the_label_and_the_text_is_not_asked(self):
        self.check(["remote"], location="Remote", description="Teams may choose a hybrid work schedule.")
        self.check(["hybrid"], location="Hybrid", description="This is a fully remote position.")
        self.check(["remote"], location="Austin, TX (Remote)", description="We are a hybrid workplace.")

    def test_hybrid_wording_in_the_text_beats_work_from_home_days_but_not_a_remote_position(self):
        self.check(["hybrid"], description="Hybrid work schedule: in the office three days a week, work from home the rest.")
        self.check(["remote", "hybrid"], description="This is a remote position. Some teams use a hybrid work model.")

    def test_not_remote_beats_a_stray_remote_phrase_but_not_a_remote_location(self):
        self.check(["onsite"], description="Fully remote candidates need not apply. This is not a remote position.")
        self.check(["remote"], location="Remote - US", description="This is not a remote-eligible team.")

    def test_hybrid_is_not_also_in_person_and_remote_work_is_not_also_in_person(self):
        self.check(["hybrid"], description="This is a hybrid role. You must work on-site on Tuesdays and Thursdays.")
        self.check(["hybrid"], description="This is a hybrid on-site role at our headquarters.")
        self.check(["hybrid"], description="In-person attendance is expected at least 3 days per week.")
        self.check(["remote"], description="Fully remote. Our office in Austin is open if you want it, and you may work on-site there.")

    def test_words_that_look_like_remote_but_are_not(self):
        self.check([], title="Remote Sensing Engineer")
        self.check([], title="Remote Monitoring Specialist")
        self.check([], description="We do not offer remote-only roles.")

    def test_every_pattern_has_key_words_that_gate_it(self):
        for mode in workmode.TEXT:
            self.assertEqual(len(workmode.GATES[mode]), len(workmode.TEXT[mode]), mode)

    def test_the_key_word_gates_never_hide_a_match(self):
        """A gate is only a shortcut: for every sentence the patterns recognise, the gate must let it through."""
        samples = {
            "remote": ["this is a fully remote position.", "remote-first company", "work from home with a laptop", "you may work remotely",
                       "telework: remote", "remote work is available", "eligible for remote work", "this role is remote"],
            "hybrid": ["a hybrid work schedule", "telework: hybrid", "(hybrid)", "three days a week in the office", "in the office three days",
                       "in-person attendance at least 3 days per week", "partially remote"],
            "onsite": ["this role is on-site", "an onsite position", "work in the office", "in-person role", "telework: in office", "must work on-site"],
        }
        for mode, sentences in samples.items():
            for sentence in sentences:
                self.assertTrue(workmode.says(mode, sentence), (mode, sentence))

    def test_only_the_start_of_a_long_text_is_read(self):
        self.check([], description=("Build things. " * 2000) + "This is a fully remote position.")


if __name__ == "__main__":
    unittest.main()
