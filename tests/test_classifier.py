import unittest

from jobbot.classifier import classify


class ClassifyTests(unittest.TestCase):
    def test_classifies_python_backend_role(self):
        self.assertEqual(
            classify(
                "Senior Python Backend Engineer",
                "Build FastAPI services backed by PostgreSQL and SQLAlchemy.",
            ),
            "backend_python",
        )

    def test_classifies_data_engineering_role(self):
        self.assertEqual(
            classify(
                "Data Engineer",
                "Own Airflow ETL pipelines using Spark, Kafka, and dbt.",
            ),
            "data_engineering",
        )

    def test_classifies_ml_engineering_role(self):
        self.assertEqual(
            classify(
                "Machine Learning Engineer",
                "Train and deploy PyTorch models with an MLOps workflow.",
            ),
            "ml_engineering",
        )

    def test_classifies_devops_role(self):
        self.assertEqual(
            classify(
                "DevOps Engineer",
                "Manage Kubernetes, Terraform, Helm, and CI/CD infrastructure.",
            ),
            "devops",
        )

    def test_title_outweighs_incidental_description_keyword(self):
        self.assertEqual(
            classify(
                "Python Backend Developer",
                "Deploy a Django service to Kubernetes.",
            ),
            "backend_python",
        )

    def test_returns_other_without_a_match(self):
        self.assertEqual(
            classify("Account Executive", "Own enterprise sales accounts."),
            "other",
        )

    def test_supports_custom_weights(self):
        self.assertEqual(
            classify("Rust Engineer", "Systems role", {"systems": {"rust": 3}}),
            "systems",
        )


if __name__ == "__main__":
    unittest.main()
