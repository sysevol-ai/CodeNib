<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Try public source with your OpenRouter account

A precomputed story Wiki can offer an optional **Find code** trial on its published
repository. Browsing its pages remains free of model calls. When you connect
OpenRouter and submit a question, your browser asks a model to plan bounded grep
searches, then asks Jev to rank the matching source. Results link to file and
line ranges at the Wiki's published commit.

**Source-checkout preview:** this feature is not in PyPI 0.2.3 and has not been
deployed to the public demo. The complete browser flow has passed with real
Requests source and fixture provider responses. Actual provider consent and
production-host acceptance remain release gates. Those checks do not establish
retrieval quality or coding-agent token savings.

## Connect and query

On a Wiki that explicitly enables the trial:

1. Select **Find code with OpenRouter** and review the repository, models and
   source disclosure.
2. Select **Connect OpenRouter**. Review the account and key settings on
   OpenRouter's authorization page. Return to the original Wiki tab afterward.
3. Enter a concrete question and select **Find code**. Authorization alone
   does not start a paid query.
4. Inspect the ranked source and commit-pinned citations. Select **Use with
   your local agent** to try [grep → Jev on your own repository](grep-jev.md).

The browser uses [OpenRouter's S256 PKCE flow](https://openrouter.ai/docs/guides/overview/auth/oauth).
It exchanges the one-use grant directly with OpenRouter. The credential stays
in the initiating tab's memory for at most ten minutes; it is not placed in
React state, a URL, cookies, browser storage, a static export or the CodeNib
source-service request. Reloading, navigating away from the trial or selecting
**Disconnect** removes the application's in-memory reference and cancels its
pending requests. This is not secure erasure of browser memory.

The question and directory overview go to the planning provider. The question
and selected public code snippets go to OpenRouter's Jev endpoint. The CodeNib
CPU service receives the planned search expressions and published source
fingerprint. Search expressions can reveal terms from your question; do not
enter private information into a public trial.

The interface reports provider costs and stops subsequent calls after $0.10
per query or $0.50 per connection. These are application thresholds: an
in-flight call can exceed them, and cancelling a call does not undo a charge.
Missing usage information stops further queries until you deliberately
reconnect. Failed model calls are never retried automatically. Use the linked
OpenRouter key settings to configure a provider credit limit and review usage.

Disconnecting does **not** revoke the provider key or give it a ten-minute
provider lifetime. Delete that key in OpenRouter settings to revoke it. The
browser rejects management/provisioning keys and does not request permission
to manage other keys. Local CLI credentials are independent; see
[local authorization and logout](openrouter.md).

If a key was created but metadata validation or transport then fails, the page
retains a safe OpenRouter settings link to review or revoke that unused key.
It does not keep the key as a connected credential. A normal inference key
must be explicitly confirmed by the provider's management-key flag.

## Enable the trial on a static Wiki

Keep the static host and source service separate from the operator's Wiki
generation server. First [export a verified static Wiki](../github_pages.md#publish-an-existing-story-wiki)
and retain the exact source checkout and source-selection manifest used by that
export. Publish only a public repository that you intend readers to search.
No private repository submission or hosted indexing route is provided.

Use one reviewed source revision for the frontend, exporter and service. Build
the frontend, then opt in when exporting:

```bash
make web-deps
npm --prefix web run build
codenib export --wiki-config /path/to/qa_config.yaml \
  --wiki-repo psf__requests --output /path/to/site \
  --frontend-dir web/dist --base-path / \
  --trial-api-base https://source.example.com
```

The API option must be an exact HTTPS origin, with no path, credentials, query
or fragment. HTTP `localhost` and `127.0.0.1` origins are supported for local
acceptance. IPv6 address literals are rejected because browsers cannot match
them in a [CSP host source](https://www.w3.org/TR/CSP/#match-hosts); use a hostname
for an IPv6 listener. Without
the option, Ask retains the static site's local-agent handoff. Ordinary Wiki
navigation never contacts the trial service or a model. Live local Wiki Ask
keeps its existing behavior.

The trial requires `--wiki-config` and `--wiki-repo`, with an explicit
`owner/name` and full commit in the registry entry. Index-derived exports
currently publish a local directory label and reject the trial option before
repository work; they keep the local-agent handoff. The exporter does not
infer a public GitHub identity from ambient Git configuration.

Create a separate source-service configuration. Copy the repository slug,
commit and source fingerprint from the verified site's `codenib-static.json`;
replace the placeholders below with those exact values. The repository id
must match the exported Wiki's id. The root is an absolute path to that
publication's checkout, with its original exclusion policy:

```json
{
  "origins": ["https://demo.example.com"],
  "hosts": ["source.example.com"],
  "repositories": [{
    "id": "psf__requests",
    "repository": "psf/requests",
    "root": "/srv/codenib/public/requests",
    "commit": "<40-character published commit>",
    "source_fingerprint": "<published sha256-v2 fingerprint>"
  }]
}
```

The CPU service needs the lightweight `grep` extra and ripgrep. It does not
need an OpenRouter key, GPU, embeddings, a running generation service or a
Wiki database. Start a single worker on loopback behind your HTTPS proxy:

```bash
python -m codenib.web.public_trial \
  --config /srv/codenib/public-trial.json --port 8001 \
  --trusted-proxy 127.0.0.1
```

`--trusted-proxy` is optional, repeatable and accepts only exact IP addresses.
Omit it for a direct local test. By default forwarded headers are ignored.
For an IPv6 loopback service, bind with `--host ::1`, put `"localhost"` in
`hosts`, and export with an origin such as `http://localhost:8001`.
Configure your proxy to replace untrusted client forwarding headers, preserve
the configured public Host, and forward only these two routes:

| Route | Work |
| --- | --- |
| `GET /trial/repos/{id}` | Verify published source and return bounded planning metadata. |
| `POST /trial/repos/{id}/candidates` | Run the validated grep plan and return source-checked candidates. |

The app admits two concurrent source workers, six requests per client IP per
minute and sixty requests globally per minute. These limits are per process;
running multiple workers multiplies them. Limit upstream requests as needed
without adding workers. Bodies are capped at 16 KiB, source at 5,000 files and
32 MiB, responses at 3 MiB, and a source request has a 30-second deadline.
Only the configured repositories are accessible. Changed source, stale
fingerprints, unexpected origins/hosts and requests carrying credentials are
rejected. It never falls back to a model or operator key. Access logging is
disabled by the supplied launcher.

## Host and validate the credential boundary

The opt-in export installs a content security policy that permits scripts from
the static origin and connections only to that origin, OpenRouter and the
explicit source-service origin. The callback has its own stricter policy.
Inline scripts and event handlers are blocked; inline styles remain enabled
for the existing Wiki layout. Keep all frontend assets on a trusted dedicated
origin with no analytics, third-party scripts or user-uploaded JavaScript.
In-memory keys are still accessible to compromised same-origin code or browser
extensions; PKCE and CSP do not make a compromised frontend safe.

Serve HTTPS and the export's real `openrouter-callback.html` and
`openrouter-callback.js` files. Do not rewrite those files or missing assets
to the SPA entry point. The callback uses a nonce-scoped BroadcastChannel and
does not require a popup opener. It removes the grant from the address bar and
loads no external assets. The callback URL initially carries a short-lived
authorization code, so exclude its query string from CDN/proxy/access logs.
The credential itself never passes through that URL or your host.

Add `Content-Security-Policy: frame-ancestors 'none'` and
`Referrer-Policy: no-referrer` as response headers. The HTML policy cannot
enforce `frame-ancestors`. Do not relax the generated script/connection policy
to accommodate injected host scripts. Host rewrites and response headers are
deployment configuration, not supplied by the export command.

Before publishing the trial, verify real provider consent, denial, callback
replay/expiry, cancellation, credit limits and disconnect on that HTTPS host.
Browse every precomputed Wiki page with the API unavailable; browsing should
still work. Inspect network requests to confirm source requests carry no
credential and only deliberate queries call the provider. Verify both desktop
and mobile source links and the local-agent fallback. Keep the trial disabled
if a host changes its CSP or callback handling.
