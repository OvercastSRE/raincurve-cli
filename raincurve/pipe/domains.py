from __future__ import annotations

PIPE_HANDLED: set[str] = {
    "stripe",
    "twilio",
    "sendgrid",
    "resend",
    "plaid",
    "mailgun",
    "postmark",
    "svix",
    "paypal",
    "lemonsqueezy",
    "slack",
    "github-api",
    "discord",
}

API_ENV_WIRING: dict[str, dict[str, str]] = {
    "stripe": {
        "STRIPE_API_KEY": "sk_test_pipe_{port}",
        "STRIPE_SECRET_KEY": "sk_test_pipe_{port}",
        "STRIPE_API_BASE": "{base}/stripe",
        "STRIPE_PUBLISHABLE_KEY": "pk_test_pipe_{port}",
    },
    "twilio": {
        "TWILIO_ACCOUNT_SID": "AC_pipe_mock",
        "TWILIO_AUTH_TOKEN": "pipe_mock_token",
        "TWILIO_API_BASE": "{base}/twilio",
    },
    "sendgrid": {
        "SENDGRID_API_KEY": "SG.pipe_mock",
        "SENDGRID_API_BASE": "{base}/sendgrid",
    },
    "auth0": {
        "AUTH0_DOMAIN": "localhost:{port}",
        "AUTH0_CLIENT_ID": "pipe_mock_client",
        "AUTH0_CLIENT_SECRET": "pipe_mock_secret",
        "AUTH0_ISSUER_BASE_URL": "{base}/auth0",
        "AUTH0_BASE_URL": "{base}/auth0",
    },
    "google-oauth": {
        "GOOGLE_CLIENT_ID": "pipe_mock_client.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "pipe_mock_secret",
        "GOOGLE_AUTH_BASE_URL": "{base}/google-oauth",
    },
    "google-calendar": {
        "GOOGLE_CALENDAR_API_BASE": "{base}/google-calendar",
    },
    "clerk": {
        "CLERK_SECRET_KEY": "sk_test_pipe_mock",
        "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY": "pk_test_pipe_mock",
        "CLERK_API_URL": "{base}/clerk",
    },
    "resend": {
        "RESEND_API_KEY": "re_pipe_mock",
        "RESEND_API_BASE": "{base}/resend",
    },
    "plaid": {
        "PLAID_CLIENT_ID": "pipe_mock_client",
        "PLAID_SECRET": "pipe_mock_secret",
        "PLAID_ENV": "sandbox",
        "PLAID_BASE_URL": "{base}/plaid",
    },
    "mailgun": {
        "MAILGUN_API_KEY": "pipe_mock_key",
        "MAILGUN_DOMAIN": "sandbox.pipe.local",
        "MAILGUN_API_BASE": "{base}/mailgun",
    },
    "postmark": {
        "POSTMARK_API_KEY": "pipe_mock_token",
        "POSTMARK_SERVER_TOKEN": "pipe_mock_token",
        "POSTMARK_API_BASE": "{base}/postmark",
    },
    "svix": {
        "SVIX_API_KEY": "pipe_mock_key",
        "SVIX_TOKEN": "pipe_mock_token",
        "SVIX_SERVER_URL": "{base}/svix",
    },
    "paypal": {
        "PAYPAL_CLIENT_ID": "pipe_mock_client",
        "PAYPAL_CLIENT_SECRET": "pipe_mock_secret",
        "PAYPAL_API_BASE": "{base}/paypal",
    },
    "lemonsqueezy": {
        "LEMONSQUEEZY_API_KEY": "pipe_mock_key",
        "LEMONSQUEEZY_API_BASE": "{base}/lemonsqueezy",
    },
    "slack": {
        "SLACK_BOT_TOKEN": "xoxb-pipe-mock",
        "SLACK_API_BASE": "{base}/slack",
    },
    "github-api": {
        "GITHUB_TOKEN": "ghp_pipe_mock",
        "GITHUB_API_URL": "{base}/github",
    },
    "discord": {
        "DISCORD_TOKEN": "pipe_mock_token",
        "DISCORD_API_BASE": "{base}/discord",
    },
}


def get_env_wiring(api: str, base_url: str, port: int) -> dict[str, str]:
    template = API_ENV_WIRING.get(api, {})
    return {k: v.format(base=base_url, port=port) for k, v in template.items()}
