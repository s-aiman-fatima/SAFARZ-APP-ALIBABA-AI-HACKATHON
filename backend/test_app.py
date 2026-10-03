import os
import unittest

os.environ["GEOCODING_ENABLED"] = "0"

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app import Bus, Route, RouteStop, Stop, create_app


class SafarzApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.groq_api_key = os.environ.pop("GROQ_API_KEY", None)
        cls.ai_api_key = os.environ.pop("AI_API_KEY", None)
        cls.app = create_app("sqlite:///:memory:")
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if cls.groq_api_key is not None:
            os.environ["GROQ_API_KEY"] = cls.groq_api_key
        if cls.ai_api_key is not None:
            os.environ["AI_API_KEY"] = cls.ai_api_key

    def test_extracted_data_is_seeded(self):
        with Session(self.app.config["DATABASE_ENGINE"]) as session:
            self.assertGreaterEqual(len(session.scalars(select(Stop)).all()), 259)
            self.assertGreaterEqual(len(session.scalars(select(Bus)).all()), 16)
            self.assertGreaterEqual(len(session.scalars(select(Route)).all()), 16)
            self.assertGreaterEqual(len(session.scalars(select(RouteStop)).all()), 356)

    def test_stop_search_supports_aliases(self):
        response = self.client.get("/api/stops?q=sadar")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["stops"][0]["name"], "Saddar")

    def test_route_search_returns_ordered_extracted_route(self):
        response = self.client.get("/api/routes?from=Khokrapar&to=Tower")
        self.assertEqual(response.status_code, 200)
        route = next(item for item in response.json["routes"] if item["routeCode"] == "R-1")
        self.assertEqual(route["stops"][0], "Khokrapar")
        self.assertEqual(route["stops"][-1], "Tower")
        self.assertEqual(route["category"], "red_bus")
        self.assertTrue(route["imageUrl"])

    def test_multiple_routes_are_ranked_and_use_attached_assets(self):
        response = self.client.get("/api/routes?from=Malir%20Halt&to=Tower")
        self.assertGreaterEqual(len(response.json["routes"]), 2)
        self.assertTrue(any(route["changes"] == 0 for route in response.json["routes"]))
        self.assertTrue(any(route["changes"] > 0 for route in response.json["routes"]))
        self.assertTrue(response.json["routes"][0]["recommended"])
        self.assertIsNone(response.json["routes"][0]["distanceKm"])
        self.assertTrue(response.json["routes"][0]["imageUrl"].startswith("/assets/"))

    def test_route_validation(self):
        response = self.client.get("/api/routes?from=Khokrapar")
        self.assertEqual(response.status_code, 400)

    def test_route_ranking_falls_back_without_ai_credentials(self):
        response = self.client.get("/api/routes?from=Malir%20Halt&to=Tower")
        self.assertTrue(response.json["routes"])
        self.assertEqual(response.json["routes"][0]["rankingSource"], "database")

    def test_chatbot_is_grounded_without_groq_key(self):
        response = self.client.post("/api/chat", json={"message": "suggest buses", "from": "Malir Halt", "to": "Tower"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["source"], "database")
        self.assertIn("R-1", response.json["answer"])

    def test_chatbot_answers_general_help_without_locations(self):
        response = self.client.post("/api/chat", json={"message": "How do I use Safarz?"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("imported Karachi route data", response.json["answer"])

    def test_chatbot_does_not_use_unrelated_trip_defaults(self):
        response = self.client.post(
            "/api/chat",
            json={
                "message": "How do I use Safarz?",
                "from": "Gulshan Chowrangi",
                "to": "Tower",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json["fromStop"])
        self.assertIsNone(response.json["toStop"])
        self.assertNotIn("Gulshan Chowrangi to Tower", response.json["answer"])

    def test_chatbot_can_answer_bus_name_questions(self):
        response = self.client.post("/api/chat", json={"message": "What route does Masood take?"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["routes"])
        self.assertEqual({route["routeCode"] for route in response.json["routes"]}, {"Masood"})
        self.assertIn("Masood", response.json["answer"])

    def test_route_search_can_recommend_one_transfer(self):
        response = self.client.get("/api/routes?from=Gulshan%20Chowrangi&to=Saddar")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["routes"])
        self.assertEqual(response.json["routes"][0]["changes"], 1)
        self.assertEqual(len(response.json["routes"][0]["legs"]), 2)
        connecting = response.json["routes"][0]
        self.assertEqual(connecting["busCodes"], [leg["routeCode"] for leg in connecting["legs"]])
        self.assertNotIn("+", connecting["routeCode"])
        self.assertTrue(connecting["transferStop"])
        self.assertIsNone(response.json["routes"][0]["distanceKm"])
        self.assertTrue(response.json["routes"][0]["recommended"])

    def test_nipa_shorthand_finds_direct_gulshan_route(self):
        response = self.client.get("/api/routes?from=NIPA&to=Gulshan%20Chowrangi")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["routes"])
        self.assertEqual(response.json["routes"][0]["changes"], 0)
        self.assertIn("D-7", [route["routeCode"] for route in response.json["routes"] if route["changes"] == 0])

    def test_reverse_direction_keeps_direct_bus(self):
        response = self.client.get("/api/routes?from=Tower&to=Khokrapar")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["routes"])
        self.assertEqual(response.json["routes"][0]["changes"], 0)
        self.assertEqual(response.json["routes"][0]["routeCode"], "R-1")


if __name__ == "__main__":
    unittest.main()