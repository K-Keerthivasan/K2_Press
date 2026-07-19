from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filter import _build_score_system


class FilterScoringGuidanceTests(unittest.TestCase):
    def test_build_score_system_includes_brand_guidance(self):
        brand = {"name": "JKR", "scoring_guidance": "Prioritize gaming stories."}
        profile = "JKR is a gaming page."

        system = _build_score_system(profile, brand_name="JKR", brand=brand)

        self.assertIn("Prioritize gaming stories.", system)
        self.assertIn("BRAND SCORING GUIDANCE", system)
        self.assertIn("JKR is a gaming page.", system)


if __name__ == "__main__":
    unittest.main()
