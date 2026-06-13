import pytest

from src.services.deep_research_service import (
    DeepResearchJob,
    DeepResearchJobStore,
    DeepResearchRequest,
    DeepResearchRunner,
    DeepResearchSearchClient,
    DeepResearchSource,
)


class FakeLLM:
    async def generate(self, prompt: str, *, max_tokens: int = 2048) -> str:
        if "`Q:" in prompt or "検索クエリ" in prompt:
            return "Q: local deep research architecture\nQ: citation grounded reports"
        return "結論: 反復検索と引用付き統合が中核です [1]。"


class FakeSearchClient(DeepResearchSearchClient):
    async def search(
        self,
        query: str,
        *,
        engines,
        max_results_per_engine: int,
        project_id=None,
        include_local_knowledge: bool = False,
        actor_user_id=None,
        is_admin: bool = False,
    ):
        return [
            DeepResearchSource(
                id=0,
                title=f"Source for {query}",
                url=f"https://example.test/{abs(hash(query))}",
                snippet=f"Evidence about {query}",
                engine="fake",
                query=query,
            )
        ]


@pytest.mark.asyncio
async def test_deep_research_runner_completes_with_sources(tmp_path):
    store = DeepResearchJobStore(tmp_path)
    runner = DeepResearchRunner(
        config={},
        store=store,
        search_client=FakeSearchClient({}),
        llm_factory=lambda _user_id: FakeLLM(),
    )
    job = DeepResearchJob(id="job-1", user_id="user-1", query="Deep research")

    result = await runner.run(
        job,
        DeepResearchRequest(
            query="Deep research",
            mode="quick",
            max_iterations=1,
            questions_per_iteration=2,
            engines=["fake"],
        ),
    )

    assert result.status == "completed"
    assert result.progress == 100
    assert len(result.sources) == 2
    assert result.questions_by_iteration["1"][0] == "Deep research"
    assert "参考ソース" in result.report_markdown
    assert store.load("job-1").status == "completed"


def test_deep_research_request_normalizes_limits():
    request = DeepResearchRequest(
        query="  topic  ",
        mode="unknown",
        max_iterations=99,
        questions_per_iteration=99,
        max_results_per_query=99,
        engines=[],
        actor_user_id="00000000-0000-0000-0000-000000000001",
        is_admin=True,
    ).normalized()

    assert request.query == "topic"
    assert request.mode == "detailed"
    assert request.max_iterations == 8
    assert request.questions_per_iteration == 6
    assert request.max_results_per_query == 10
    assert request.actor_user_id == "00000000-0000-0000-0000-000000000001"
    assert request.is_admin is True
