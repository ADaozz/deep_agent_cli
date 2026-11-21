from agent.redaction import replace_known_secrets


def test_longest_first_and_stable_ties():
    assert replace_known_secrets("token-long token", ["token", "", "token-long", "token"]) == "[REDACTED] [REDACTED]"
    assert replace_known_secrets("abc", iter(["bc", "ab"])) == "a[REDACTED]"
