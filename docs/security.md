# Security notes

> **No headless browser, on purpose.** The deploy host sits on a LAN with an unauthenticated
> Portainer API and other admin surfaces with no auth. A browser rendering attacker-controlled
> pages would do its own DNS, follow its own redirects, and load its own subresources — none of
> which pass through any guard this app writes. URLs are fetched with a pinned HTTP client instead
> (`net_guard.py`), and thumbnails come from `og:image`/`twitter:image` meta tags.
