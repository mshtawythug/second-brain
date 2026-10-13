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

Earlier docs recommended `--host 0.0.0.0` (every interface). **That cannot serve a phone.** The
server only accepts requests whose `Host` header is on an allowlist, and that allowlist is what
defeats DNS-rebinding attacks. A wildcard bind gets the loopback allowlist, so a phone that
reaches the machine at its LAN address is refused with **400 Invalid host header**. Writes fail
on that bind from every device. The guard is correct, so bind the LAN address itself. `brain ui`
prints a warning if you use the wildcard bind.

For the same reason, open the **IP address** URL that `brain ui` prints, not the machine's
`<name>.local` name: the allowlist holds the address you bound, nothing else.

## How the token travels

The token rides in the URL **fragment** (`#t=…`). Browsers never send fragments to the server,
never write them to access logs, and never leak them in a `Referer`. The page reads the token,
keeps it **in memory only**, and removes it from the address bar right away. Every API call
then carries it in a request header.

Two consequences follow:

- **A reload loses the token.** If the phone reloads the tab, or the browser evicts it in the
  background, every request is refused (403) until you open the full `#t=` URL again.
  **Bookmark the URL exactly as `brain ui` printed it**, not the address bar after the page has
  loaded.
- **A minted token changes on every run**, so that bookmark breaks when you restart `brain ui`.
  For a stable bookmark, pass your own `--token`. The trade-off is that a secret typed on the
  command line lands in your shell history and is visible in `ps` while the server runs.

## Caveats — read these before you turn it on

- **Plain HTTP. Use it only on networks you trust.** Nothing is encrypted, so anyone who can see
  the traffic can read both the pages and the token. Your home Wi-Fi is fine. A café or hotel
  network is not.
- **Anyone with the URL can read your brain.** The token is the only gate, so treat the printed
  URL like a password.
- **Prefer `--read-only` for phone reading.** A phone on the LAN then has no write path at all.
- **Confidential documents are withheld off loopback.** On a LAN bind, the bodies of documents
  marked `sensitivity: confidential` are not served unless you pass `--include-confidential`.
  The startup panel states which mode you got. On loopback they are always served.
- **The machine must keep `brain ui` running.** It runs in the foreground and has no daemon, by
  design: a write-capable server should not outlive your attention. Closing the terminal, or the
  machine sleeping, ends phone access.
- **The firewall may ask.** On macOS the first LAN bind can prompt you to allow incoming
  connections for Python. Allow it, or the phone will time out.

## The wiki is still there

Until phase 8 of the wiki → `brain ui` consolidation, the Quartz wiki keeps building and Caddy
keeps serving it as before, including on the LAN with no token. It is no longer where the
daily loop points: `brain-up` no longer opens it unless you set `BRAIN_OPEN_BROWSER=1`. It stays
available as a fallback, for example when Postgres is down, until you decide to retire it.
