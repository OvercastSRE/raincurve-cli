from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import httpx

from raincurve.pipe.domains import PIPE_HANDLED, get_env_wiring
from raincurve.pipe.mock_agent import MockAgent
from raincurve.pipe.models import InterceptedRequest, MockResponse
from raincurve.pipe.server import PipeServer, ResponseCache
from raincurve.pipe.state import StateStore


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------


class TestStateStore:
    def test_put_and_get(self):
        store = StateStore()
        store.put("stripe", "customers", "cus_1", {"id": "cus_1", "email": "a@b.com"})
        assert store.get("stripe", "customers", "cus_1")["email"] == "a@b.com"

    def test_get_missing(self):
        assert StateStore().get("stripe", "customers", "nope") is None

    def test_list_all(self):
        store = StateStore()
        store.put("stripe", "customers", "cus_1", {"id": "cus_1"})
        store.put("stripe", "customers", "cus_2", {"id": "cus_2"})
        assert len(store.list_all("stripe", "customers")) == 2

    def test_list_all_empty(self):
        assert StateStore().list_all("stripe", "customers") == []

    def test_delete(self):
        store = StateStore()
        store.put("stripe", "customers", "cus_1", {"id": "cus_1"})
        assert store.delete("stripe", "customers", "cus_1") is True
        assert store.get("stripe", "customers", "cus_1") is None
        assert store.delete("stripe", "customers", "cus_1") is False

    def test_dump_filters_by_api(self):
        store = StateStore()
        store.put("stripe", "customers", "cus_1", {"id": "cus_1"})
        store.put("stripe", "charges", "ch_1", {"id": "ch_1"})
        store.put("twilio", "messages", "SM1", {"sid": "SM1"})

        dump = store.dump("stripe")
        assert set(dump.keys()) == {"customers", "charges"}
        assert len(dump["customers"]) == 1

    def test_dump_empty(self):
        assert StateStore().dump("stripe") == {}

    def test_clear(self):
        store = StateStore()
        store.put("stripe", "customers", "cus_1", {"id": "cus_1"})
        store.clear()
        assert store.list_all("stripe", "customers") == []

    def test_thread_safety(self):
        store = StateStore()
        errors: list[Exception] = []

        def writer(prefix: str):
            try:
                for i in range(100):
                    store.put("stripe", "customers", f"{prefix}_{i}", {"id": f"{prefix}_{i}"})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f"t{n}",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store.list_all("stripe", "customers")) == 400


# ---------------------------------------------------------------------------
# MockAgent
# ---------------------------------------------------------------------------


def _fake_client(content: str) -> MagicMock:
    client = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    client.chat.completions.create.return_value = resp
    return client


class TestMockAgent:
    def test_create_customer(self):
        payload = {
            "status": 200,
            "body": {"id": "cus_abc", "object": "customer", "email": "a@b.com"},
            "state_writes": {
                "customers": {"cus_abc": {"id": "cus_abc", "email": "a@b.com"}}
            },
        }
        agent = MockAgent(client=_fake_client(json.dumps(payload)))
        state = StateStore()
        req = InterceptedRequest(api="stripe", method="POST", path="/v1/customers", body="email=a@b.com")
        resp = agent.generate_response(req, state)

        assert resp.status == 200
        assert resp.body["id"] == "cus_abc"
        assert state.get("stripe", "customers", "cus_abc") is not None

    def test_strips_markdown_fences(self):
        payload = {"status": 200, "body": {"id": "cus_1"}, "state_writes": {}}
        raw = "```json\n" + json.dumps(payload) + "\n```"
        agent = MockAgent(client=_fake_client(raw))
        resp = agent.generate_response(
            InterceptedRequest(api="stripe", method="GET", path="/v1/customers/cus_1"),
            StateStore(),
        )
        assert resp.status == 200
        assert resp.body["id"] == "cus_1"

    def test_invalid_json_returns_500(self):
        agent = MockAgent(client=_fake_client("this is not json"))
        resp = agent.generate_response(
            InterceptedRequest(api="stripe", method="GET", path="/v1/customers"),
            StateStore(),
        )
        assert resp.status == 500

    def test_null_state_write_deletes(self):
        agent = MockAgent(
            client=_fake_client(
                json.dumps({
                    "status": 200,
                    "body": {"id": "cus_1", "deleted": True},
                    "state_writes": {"customers": {"cus_1": None}},
                })
            )
        )
        state = StateStore()
        state.put("stripe", "customers", "cus_1", {"id": "cus_1"})
        agent.generate_response(
            InterceptedRequest(api="stripe", method="DELETE", path="/v1/customers/cus_1"),
            state,
        )
        assert state.get("stripe", "customers", "cus_1") is None

    def test_true_shorthand_stores_body(self):
        payload = {
            "status": 200,
            "body": {"id": "cus_short", "object": "customer", "email": "x@y.com"},
            "state_writes": {"customers": {"cus_short": True}},
        }
        agent = MockAgent(client=_fake_client(json.dumps(payload)))
        state = StateStore()
        agent.generate_response(
            InterceptedRequest(api="stripe", method="POST", path="/v1/customers", body="email=x@y.com"),
            state,
        )
        stored = state.get("stripe", "customers", "cus_short")
        assert stored is not None
        assert stored["id"] == "cus_short"

    def test_repair_truncated_json(self):
        truncated = '{"status": 200, "body": {"id": "cus_1", "object": "customer"}, "state_writes": {}'
        agent = MockAgent(client=_fake_client(truncated))
        resp = agent.generate_response(
            InterceptedRequest(api="stripe", method="POST", path="/v1/customers"),
            StateStore(),
        )
        assert resp.status == 200
        assert resp.body["id"] == "cus_1"

    def test_large_state_gets_summarised(self):
        client = _fake_client(json.dumps({"status": 200, "body": {}, "state_writes": {}}))
        agent = MockAgent(client=client)
        state = StateStore()
        for i in range(200):
            state.put("stripe", "customers", f"cus_{i}", {"id": f"cus_{i}", "data": "x" * 50})

        agent.generate_response(
            InterceptedRequest(api="stripe", method="GET", path="/v1/customers"),
            state,
        )
        call_args = client.chat.completions.create.call_args
        system_msg = call_args.kwargs["messages"][0]["content"]
        assert "count" in system_msg


# ---------------------------------------------------------------------------
# Domains / env wiring
# ---------------------------------------------------------------------------


class TestDomains:
    def test_stripe_wiring(self):
        wiring = get_env_wiring("stripe", "http://host.docker.internal:19877", 19877)
        assert wiring["STRIPE_API_BASE"] == "http://host.docker.internal:19877/stripe"
        assert "19877" in wiring["STRIPE_API_KEY"]

    def test_twilio_wiring(self):
        wiring = get_env_wiring("twilio", "http://h:9999", 9999)
        assert wiring["TWILIO_API_BASE"] == "http://h:9999/twilio"

    def test_unknown_api_returns_empty(self):
        assert get_env_wiring("nope", "http://h:1", 1) == {}


# ---------------------------------------------------------------------------
# PipeServer (integration)
# ---------------------------------------------------------------------------


class TestPipeServer:
    def _make_server(self, llm_response: dict) -> PipeServer:
        client = _fake_client(json.dumps(llm_response))
        server = PipeServer(client=client, port=0)
        return server

    def test_stripe_create_customer(self):
        server = self._make_server({
            "status": 200,
            "body": {"id": "cus_test", "object": "customer"},
            "state_writes": {},
        })
        server.start()
        try:
            resp = httpx.post(
                f"http://127.0.0.1:{server.port}/stripe/v1/customers",
                data={"email": "test@test.com"},
                timeout=10,
            )
            assert resp.status_code == 200
            assert resp.json()["id"] == "cus_test"
        finally:
            server.stop()

    def test_twilio_send_sms(self):
        server = self._make_server({
            "status": 201,
            "body": {"sid": "SM_abc", "status": "queued"},
            "state_writes": {},
        })
        server.start()
        try:
            resp = httpx.post(
                f"http://127.0.0.1:{server.port}/twilio/2010-04-01/Accounts/AC123/Messages.json",
                data={"To": "+1234", "From": "+5678", "Body": "hi"},
                timeout=10,
            )
            assert resp.status_code == 201
            assert resp.json()["sid"] == "SM_abc"
        finally:
            server.stop()

    def test_missing_api_prefix(self):
        server = self._make_server({"status": 200, "body": {}, "state_writes": {}})
        server.start()
        try:
            resp = httpx.get(f"http://127.0.0.1:{server.port}/", timeout=10)
            assert resp.status_code == 404
        finally:
            server.stop()

    def test_env_wiring_for(self):
        client = _fake_client("{}")
        server = PipeServer(client=client, port=0)
        wiring = server.env_wiring_for(["stripe", "twilio"])
        assert "STRIPE_API_BASE" in wiring
        assert "TWILIO_API_BASE" in wiring
        assert str(server.port) in wiring["STRIPE_API_BASE"]
        server.server_close()

    def test_state_persists_across_requests(self):
        call_count = 0
        original_client = _fake_client("{}")

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                body = {
                    "status": 200,
                    "body": {"id": "cus_persist", "object": "customer"},
                    "state_writes": {
                        "customers": {"cus_persist": {"id": "cus_persist", "object": "customer"}}
                    },
                }
            else:
                body = {
                    "status": 200,
                    "body": {"id": "cus_persist", "object": "customer"},
                    "state_writes": {},
                }
            choice = MagicMock()
            choice.message.content = json.dumps(body)
            resp = MagicMock()
            resp.choices = [choice]
            return resp

        original_client.chat.completions.create.side_effect = side_effect

        server = PipeServer(client=original_client, port=0)
        server.start()
        try:
            httpx.post(
                f"http://127.0.0.1:{server.port}/stripe/v1/customers",
                data={"email": "test@test.com"},
                timeout=10,
            )
            assert server.state.get("stripe", "customers", "cus_persist") is not None

            httpx.get(
                f"http://127.0.0.1:{server.port}/stripe/v1/customers/cus_persist",
                timeout=10,
            )
            create_args = original_client.chat.completions.create.call_args_list
            second_system = create_args[1].kwargs["messages"][0]["content"]
            assert "cus_persist" in second_system
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# ResponseCache
# ---------------------------------------------------------------------------


class TestResponseCache:
    def test_caches_get_requests(self):
        cache = ResponseCache()
        req = InterceptedRequest(api="stripe", method="GET", path="/v1/customers")
        resp = MockResponse(status=200, body={"id": "cus_1"})

        assert cache.get(req) is None
        cache.put(req, resp)
        assert cache.get(req) is not None
        assert cache.get(req).body["id"] == "cus_1"

    def test_skips_post_requests(self):
        cache = ResponseCache()
        req = InterceptedRequest(api="stripe", method="POST", path="/v1/customers")
        resp = MockResponse(status=200, body={"id": "cus_1"})

        cache.put(req, resp)
        assert cache.get(req) is None

    def test_evicts_when_full(self):
        cache = ResponseCache(max_size=2)
        for i in range(3):
            req = InterceptedRequest(api="stripe", method="GET", path=f"/v1/cus_{i}")
            cache.put(req, MockResponse(status=200, body={"i": i}))

        first = InterceptedRequest(api="stripe", method="GET", path="/v1/cus_0")
        assert cache.get(first) is None

        last = InterceptedRequest(api="stripe", method="GET", path="/v1/cus_2")
        assert cache.get(last) is not None

    def test_get_cache_avoids_llm_call(self):
        """Cached GET responses should bypass the LLM entirely."""
        call_count = 0
        original_client = _fake_client("{}")

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            choice = MagicMock()
            choice.message.content = json.dumps({
                "status": 200,
                "body": {"cached": True},
                "state_writes": {},
            })
            resp = MagicMock()
            resp.choices = [choice]
            return resp

        original_client.chat.completions.create.side_effect = side_effect

        server = PipeServer(client=original_client, port=0)
        server.start()
        try:
            url = f"http://127.0.0.1:{server.port}/stripe/v1/balance"
            httpx.get(url, timeout=10)
            httpx.get(url, timeout=10)
            httpx.get(url, timeout=10)
            assert call_count == 1
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# PIPE_HANDLED set
# ---------------------------------------------------------------------------


class TestPipeHandled:
    def test_stripe_is_handled(self):
        assert "stripe" in PIPE_HANDLED

    def test_twilio_is_handled(self):
        assert "twilio" in PIPE_HANDLED

    def test_databases_are_not_handled(self):
        for db in ["postgres", "mysql", "redis", "mongodb"]:
            assert db not in PIPE_HANDLED
