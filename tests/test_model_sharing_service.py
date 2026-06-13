from src.services.model_sharing_service import build_redacted_prompt


def test_build_redacted_prompt_hides_obvious_sensitive_values():
    prompt = (
        "Please review ticket for tanaka@example.com. "
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz "
        r"Logs are in C:\Projects\client-project\secret.txt and http://jira.internal/browse/AOI-1"
    )

    redacted, findings = build_redacted_prompt(prompt)

    assert "tanaka@example.com" not in redacted
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in redacted
    assert r"C:\Projects\client-project\secret.txt" not in redacted
    assert "http://jira.internal/browse/AOI-1" not in redacted
    assert "[EMAIL_1]" in redacted
    assert "[SECRET_1]" in redacted
    assert "[LOCAL_PATH_1]" in redacted
    assert "[INTERNAL_URL_1]" in redacted
    assert {item["category"] for item in findings} >= {
        "EMAIL",
        "SECRET",
        "LOCAL_PATH",
        "INTERNAL_URL",
    }


def test_build_redacted_prompt_uses_proposed_prompt_and_still_scans_it():
    redacted, findings = build_redacted_prompt(
        "Original prompt with Customer A.",
        proposed_redacted_prompt="Redacted prompt for support@example.com.",
    )

    assert redacted == "Redacted prompt for [EMAIL_1]."
    assert findings == [{"category": "EMAIL", "placeholder": "[EMAIL_1]"}]


def test_build_redacted_prompt_redacts_configured_terms():
    redacted, findings = build_redacted_prompt(
        "Ask about the Example Project.",
        config={"model_sharing": {"redaction_terms": ["Example Project"]}},
    )

    assert redacted == "Ask about the [CONFIDENTIAL_TERM_1]."
    assert findings == [
        {
            "category": "CONFIDENTIAL_TERM",
            "placeholder": "[CONFIDENTIAL_TERM_1]",
        }
    ]
