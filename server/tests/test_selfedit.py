import httpx
import pytest

from agent.selfedit import SelfRepo, SelfRepoError, safe_path


class FakeRepo:
    def __init__(self, results=("pending", "success")):
        self.results = list(results)
        self.commits, self.merged, self.deleted = [], [], []

    def head(self):
        return "BASE"

    def list_files(self, sha):
        return ["server/agent/persona.py", "server/agent/core.py", "server/tests/test_agent.py"]

    def read(self, path, sha):
        return f"# {path}\n"

    def commit_branch(self, base, files, message, branch):
        self.commits.append((base, files, message, branch))
        return "NEWSHA"

    def tests(self, sha):
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return r, "https://github.com/x/runs/1"

    def merge(self, branch, message):
        self.merged.append(branch)

    def delete_branch(self, branch):
        self.deleted.append(branch)


@pytest.fixture
def edit_llm(agent):
    llm = agent.llm
    llm.edits = []

    def choose_files(request, paths):
        return ["server/agent/persona.py", "not/in/repo.py"]

    def edit_code(request, files):
        llm.edits.append((request, dict(files)))
        return {"summary": "Replies are shorter now.", "commit_message": "Shorter replies",
                "files": [{"path": "server/agent/persona.py", "content": "# shorter\n"}]}

    llm.choose_files, llm.edit_code = choose_files, edit_code
    return llm


def ask_and_confirm(agent):
    agent.llm.turn(("call", "change_own_code", {"request": "make your replies shorter"}), ("say", "ok?"))
    agent.reply("make your replies shorter")
    agent.llm.turn(("call", "confirm_pending", {}), ("say", "on it"))
    agent.reply("yes")
    assert "change my own code" in agent.llm.systems[-1]  # shown as pending on the confirming turn


def test_change_goes_live_when_tests_pass(agent, edit_llm):
    agent.selfrepo = FakeRepo()
    ask_and_confirm(agent)
    repo = agent.selfrepo
    base, files, message, branch = repo.commits[0]
    assert base == "BASE" and branch.startswith("self-edit/")
    assert files == [{"path": "server/agent/persona.py", "content": "# shorter\n"}]
    assert "Requested by text: make your replies shorter" in message
    assert edit_llm.edits[0][1] == {"server/agent/persona.py": "# server/agent/persona.py\n"}  # unknown path dropped
    assert repo.merged == [branch] and repo.deleted == [branch]
    assert "Done, tests passed" in agent.gmail.notes[-1].replace("​", "")  # email route adds markers


def test_failed_tests_change_nothing(agent, edit_llm):
    agent.selfrepo = FakeRepo(results=("pending", "failure"))
    ask_and_confirm(agent)
    assert agent.selfrepo.merged == []
    assert "tests failed" in agent.gmail.notes[-1] and "Nothing changed" in agent.gmail.notes[-1]


def test_needs_confirmation_and_setup(agent, edit_llm):
    agent.selfrepo = FakeRepo()
    agent.llm.turn(("call", "change_own_code", {"request": "x"}), ("call", "confirm_pending", {}))
    agent.reply("change yourself")
    assert agent.selfrepo.commits == []  # same-turn confirm is refused
    agent.selfrepo = None
    agent.llm.turn(("call", "change_own_code", {"request": "x"}))
    agent.reply("change yourself")
    assert "isn't set up" in agent.llm.results[-1]["error"]


@pytest.mark.parametrize("path,ok", [
    ("server/agent/core.py", True),
    ("/server/agent/core.py", True),
    (".github/workflows/tests.yml", False),
    ("render.yaml", False),
    ("server/.env", False),
    ("helper/.env.example", False),
    ("server/token.json", False),
    ("../evil.py", False),
    ("server/../../evil.py", False),
])
def test_safe_path(path, ok):
    assert (safe_path(path) is not None) == ok


def test_repo_client_flow():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        p = req.url.path
        if p.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "B"}})
        if "/git/trees/B" in p:
            return httpx.Response(200, json={"tree": [
                {"path": "server/agent/core.py", "type": "blob"},
                {"path": "server/.venv/x.py", "type": "blob"},
                {"path": "logo.png", "type": "blob"}]})
        if p.endswith("/git/commits/B"):
            return httpx.Response(200, json={"tree": {"sha": "T0"}})
        if p.endswith("/git/trees") and req.method == "POST":
            return httpx.Response(201, json={"sha": "T1"})
        if p.endswith("/git/commits") and req.method == "POST":
            return httpx.Response(201, json={"sha": "C1"})
        if p.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if p.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": [
                {"status": "completed", "conclusion": "failure", "html_url": "u"}]})
        if p.endswith("/merges"):
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})

    repo = SelfRepo("tok", "me/agent", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert repo.head() == "B"
    assert repo.list_files("B") == ["server/agent/core.py"]
    assert repo.commit_branch("B", [{"path": "server/agent/core.py", "content": "x"}], "m", "self-edit/1") == "C1"
    assert repo.tests("C1") == ("failure", "u")
    repo.merge("self-edit/1", "m")
    with pytest.raises(SelfRepoError, match="not allowed"):
        repo.commit_branch("B", [{"path": ".github/workflows/t.yml", "content": "x"}], "m", "b")
