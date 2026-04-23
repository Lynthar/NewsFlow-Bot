"""Webhook adapter — generic HTTP push for feed entries.

One adapter, many downstream targets: Slack / ntfy / Feishu (飞书) /
Work-WeChat (企业微信) / Zapier / n8n / arbitrary self-written endpoints.

Destinations are managed declaratively by `webhooks.yaml` (see
`services/webhook_sync.py`); this module only cares about *sending*.
"""

from newsflow.adapters.webhook.bot import WebhookAdapter, start_webhook, stop_webhook

__all__ = ["WebhookAdapter", "start_webhook", "stop_webhook"]
