# Reading your brain from a phone

> Part of the [Second Brain](../../README.md) docs — see [docs/README.md](../README.md) for the full index.

`brain ui` is the reading surface. By default it binds loopback, so only the machine running it
can reach it. To read from a phone or tablet on the same network, serve it to the LAN, with a
token:

```bash
brain ui --lan --read-only
```

The startup panel prints the address to open:

```text
  URL        http://192.0.2.44:8765/#t=Zq3…
  Phone      open the URL above on your phone, on this network
```

Open that exact URL on the phone. That's it.

`192.0.2.44` above is a placeholder; you'll see your machine's own address.

## What `--lan` does

- **Binds this machine's LAN address.** That is the IPv4 address the OS would use to reach the
  network. It is found from the routing table, and no packet is sent. If there is no network,
  `brain ui` exits with an error instead of quietly serving only itself.
- **Always runs with a token.** If you pass `--token <secret>`, `--lan` uses it. Otherwise it
  mints a fresh 32-character one for this run. `brain ui` never binds a non-loopback address
  without a token, and `--lan` cannot get around that rule.
- **Cannot be combined with `--host`.** Passing both is a usage error.

## The manual form

`--lan` is a shortcut for:

```bash
brain ui --host <this machine's LAN address> --token <secret>
```

Use the manual form when `--lan` picks the wrong interface, for example when a VPN is up or the
machine has several network adapters. On macOS, `ipconfig getifaddr en0` prints the Wi-Fi
address.

### Why not `--host 0.0.0.0`?

Earlier docs recommended `--host 0.0.0.0` (every interface). **That cannot serve a phone's browser.** The
server only accepts requests whose `Host` header is on an allowlist, and that allowlist is what
defeats DNS-rebinding attacks. A wildcard bind — `0.0.0.0` or `::` — gets the loopback
allowlist, so a phone that reaches the machine at its LAN address is refused with **400 Invalid
host header**. The guard is correct, so bind the LAN address itself. On a wildcard bind
`brain ui` prints a warning, and the URL it prints and opens is the loopback one, which works on
this machine only: `http://127.0.0.1:<port>/` for `0.0.0.0`, and `http://[::1]:<port>/` for
`::`.

The allowlist turns away browsers; it is not access control. It defeats DNS rebinding, because
a browser always sends the host it was pointed at, but any program on your network that can
reach the port can send a loopback `Host` header by hand. On every non-loopback bind — a
wildcard, a LAN address, `--lan` — it is the token that keeps other clients out.

`::` differs from `0.0.0.0` in one more way: it is **IPv6-only**. An IPv4 device — most phones on
a home network — cannot connect to it at all (connection refused, not 400), and on this machine
`http://127.0.0.1:<port>/` is refused too; use the `[::1]` URL it prints.

`--host` is judged by the address it will bind, not by its spelling: `0`, `127.1`, a
whitespace-padded value and any IPv6 zone such as `%en0` are refused with a message that says
why. A hostname is judged by every address it resolves to, and only `localhost` can count as
loopback — and only when it resolves to loopback addresses alone. Any other name needs `--token`,
even one that resolves to `127.0.0.1` (`ip6-localhost` and `localhost.` included): whoever
controls its DNS could point it at your machine and get past the Host check. A name is looked up once: `brain ui` binds
exactly those addresses, so the name cannot point somewhere else by the time it binds. Zones are refused even on link-local addresses, because a browser cannot open a URL that
carries one.

For the same reason, open the **IP address** URL that `brain ui` prints, not the machine's
`<name>.local` name: the allowlist holds the address you bound, nothing else — in the form a
browser sends it (for IPv6, bracketed and canonical: `--host FD00:0:0::5` is `[fd00::5]`).

## How the token travels

The token rides in the URL **fragment** (`#t=…`). Browsers never send fragments to the server,
never write them to the server's access log, and never leak them in a `Referer`. The page reads
the token, keeps it **in memory only**, and removes it from the address bar right away. Every
API call then carries it in a request header.

**The page itself never saves the token** — not in a cookie, not in browser storage. That is
deliberate: closing the tab drops it.

**But the URL is the token, and the browser can keep the URL.** Opening the printed URL can
record it in the browser's history before the page strips it, and a bookmark of it stores it
outright. Browser sync (iCloud, a Google account) then copies both to your other devices. So:

- **Treat the printed URL as a password.** Do not bookmark it on a phone you might lose or lend.
  Clear it from history if that phone is shared.
- **A minted token dies when `brain ui` stops.** A saved copy of a `--lan` URL is useless after a
  restart, which limits the damage of a leaked one.
- **A stable `--token` lives as long as you keep using it.** A bookmark of that URL keeps working
  across restarts, and keeps working for whoever holds it. Pick that trade-off on purpose. A
  secret typed on the command line also lands in your shell history and is visible in `ps`
  while the server runs.

**A reload loses the token.** Because the page holds it in memory only, a reload — or the browser
evicting the tab in the background — leaves every request refused (403) until you open the full
`#t=` URL again.

## Caveats — read these before you turn it on

- **Plain HTTP. Use it only on networks you trust.** Nothing is encrypted, so anyone who can see
  the traffic can read both the pages and the token. Your home Wi-Fi is fine. A café or hotel
  network is not.
- **Anyone with the URL can read your brain.** The token is the only gate, so treat the printed
  URL like a password.
- **Prefer `--read-only` for phone reading.** A phone on the LAN then has no write path at all.
- **Confidential documents are withheld off loopback.** On a LAN bind, the bodies of documents
  marked `sensitivity: confidential` are not served unless you pass `--include-confidential`.
  The startup panel states which mode you got. On a loopback address, or on `localhost` when it
  resolves only to loopback, they are always served; a name other than `localhost` that resolves
  to loopback is guarded like a LAN bind, so its bodies are withheld unless you pass
  `--include-confidential` too.
- **The machine must keep `brain ui` running.** It runs in the foreground and has no daemon, by
  design: a write-capable server should not outlive your attention. Closing the terminal, or the
  machine sleeping, ends phone access.
- **The firewall may ask.** On macOS the first LAN bind can prompt you to allow incoming
  connections for Python. Allow it, or the phone will time out.

## The wiki is dormant

Since phase 8 of the wiki → `brain ui` consolidation (R2), nothing rebuilds the Quartz wiki by
default. Caddy keeps serving its last build as before, including on the LAN with no token, so
what a phone sees there can be out of date. It is not where the daily loop points: `brain-up`
no longer opens it unless you set `BRAIN_OPEN_BROWSER=1`, and `brain-rebuild --only quartz`
rebuilds it on request. It stays available as a fallback, for example when Postgres is down,
until you decide to retire it.
