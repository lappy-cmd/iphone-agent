"""Print two fresh random secrets for AGENT_KEY and HELPER_KEY."""
import secrets

print(f"AGENT_KEY={secrets.token_urlsafe(32)}")
print(f"HELPER_KEY={secrets.token_urlsafe(32)}")
