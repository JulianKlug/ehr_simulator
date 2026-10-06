"""Multi tab guard (S11m) helpers: tab ids, claims, owner headers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from ehr_simulator.web.tab_guard import RENDER_ID_HEADER, TAB_ID_HEADER
from tests.conftest import _valid_value
from tests.support.cases import HTTP_OK
from tests.support.lifecycle import LifecycleHarness

TAB_A = "0b6f7c1e-3f5a-4c2d-9e8b-7a6d5c4b3a21"
TAB_B = "5d1c9a0e-8b7f-4e6d-a5c4-3b2a1f0e9d8c"


@contextmanager
def _tab(th: LifecycleHarness) -> Iterator[TestClient]:
    """A booted app whose client never auto-claims (see conftest)."""
    with th.client() as client:
        client.event_hooks["response"].clear()
        del client.tab_views
        yield client


def _owner(tab_id: str, render_id: str) -> dict[str, str]:
    return {TAB_ID_HEADER: tab_id, RENDER_ID_HEADER: render_id}


def _page(client: TestClient, patient_id: str, t_index: int = 0) -> str:
    response = client.get(f"/patient/{patient_id}/timepoint/{t_index}")
    assert response.status_code == HTTP_OK
    view = BeautifulSoup(response.text, "html.parser").select_one("#patient-view")
    return view["data-render-id"]


def _claim(client: TestClient, patient_id: str, tab_id: str, render_id: str) -> Any:
    return client.post(
        f"/case/{patient_id}/tab/claim", json={"tab_id": tab_id, "render_id": render_id}
    )


def _answer_all(client: TestClient, patient_id: str, t_index: int, headers: dict) -> None:
    from ehr_simulator.question_branching import evaluate

    questions = client.app.state.questions
    values: dict[str, str] = {}
    for q in questions.questions:
        item = evaluate(questions, values).get(q.question_id)
        if item is None or not item.required_now:
            continue
        value = _valid_value(q)
        response = client.post(
            f"/patient/{patient_id}/timepoint/{t_index}/answer",
            data={"question_id": q.question_id, "value": value},
            headers=headers,
        )
        assert response.status_code == HTTP_OK, response.text
        values[q.question_id] = value


def _post(client: TestClient, tab_id: str, events_: list[dict]) -> Any:
    return client.post("/telemetry/events", json={"tab_id": tab_id, "events": events_})
