from __future__ import annotations

from raincurve.agents.e2e_agent import E2EAgent


class TestE2EAgentVerifyDone:
    def _agent(self) -> E2EAgent:
        return E2EAgent(
            project_dir="/tmp/test",
            app_port=3000,
            login_info="admin / admin",
            routes_context="GET /api/users, POST /api/users",
        )

    def test_no_journeys_fails(self):
        agent = self._agent()
        result = agent._verify_done({"journeys": [], "summary": "nothing"})
        assert result is not None
        assert "No journeys" in result

    def test_too_few_journeys_fails(self):
        agent = self._agent()
        result = agent._verify_done({
            "journeys": [{"name": "only one", "steps": [], "passed": True}],
            "summary": "1/1",
        })
        assert result is not None
        assert "at least 3" in result

    def test_all_failed_fails(self):
        agent = self._agent()
        result = agent._verify_done({
            "journeys": [
                {"name": "j1", "steps": [], "passed": False},
                {"name": "j2", "steps": [], "passed": False},
                {"name": "j3", "steps": [], "passed": False},
            ],
            "summary": "0/3",
        })
        assert result is not None
        assert "All 3 journeys failed" in result

    def test_partial_pass_succeeds(self):
        agent = self._agent()
        result = agent._verify_done({
            "journeys": [
                {"name": "j1", "steps": [], "passed": True},
                {"name": "j2", "steps": [], "passed": False},
                {"name": "j3", "steps": [], "passed": True},
            ],
            "summary": "2/3",
        })
        assert result is None

    def test_all_pass_succeeds(self):
        agent = self._agent()
        result = agent._verify_done({
            "journeys": [
                {"name": "auth", "steps": [
                    {"method": "POST", "path": "/login", "status": 200, "passed": True, "note": "ok"},
                    {"method": "GET", "path": "/me", "status": 200, "passed": True, "note": "ok"},
                ], "passed": True},
                {"name": "crud", "steps": [
                    {"method": "POST", "path": "/items", "status": 201, "passed": True, "note": "created"},
                    {"method": "GET", "path": "/items/1", "status": 200, "passed": True, "note": "found"},
                ], "passed": True},
                {"name": "list", "steps": [
                    {"method": "GET", "path": "/items", "status": 200, "passed": True, "note": "10 items"},
                ], "passed": True},
            ],
            "summary": "3/3 journeys passed",
        })
        assert result is None
