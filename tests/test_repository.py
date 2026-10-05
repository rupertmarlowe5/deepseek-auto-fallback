from pathlib import Path

ROOT = Path(__file__).parents[1]

def test_public_contract_is_argolink_only():
    readme = (ROOT / "README.md").read_text()
    assert "argolink.io" in readme
    assert "ARGOLINK_API_KEY" in readme
    assert "deepseek-v4-pro" in readme


def test_examples_do_not_contain_credentials():
    for path in (ROOT / "examples").glob("*"):
        text = path.read_text()
        assert "sk-" not in text.lower()
        assert "Bearer ey" not in text
        assert "ARGOLINK_API_KEY" in text
