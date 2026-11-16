# SoC Print

A local web page for printing on NUS School of Computing printers: drop files on
it, pick a printer, press Print. You sign in once; after that it stays signed in.

```
./soc-print            # starts the site and opens http://127.0.0.1:6310/
./soc-print install    # optional: start at login and add an app-menu shortcut
./soc-print uninstall  # undo that
```

Needs [uv](https://docs.astral.sh/uv/) (dependencies are installed into `./.venv`
on first run), the OpenSSH client, and LibreOffice if you want to drop Word,
PowerPoint, Excel or text files. PDFs and images need nothing extra.

## How it works

SoC printing from Unix is `lpr -P <queue> <file>` on the login server
(`stu.comp.nus.edu.sg` for students, `stf` for staff), and the print servers
only accept PDF, PostScript and plain text. So SoC Print:

1. converts whatever you drop to PDF on your machine, and scales it onto the
   printer's paper size (a Letter or slide-shaped page otherwise stalls the
   printer on "wrong paper size");
2. applies page ranges, pages-per-sheet and copies to that PDF, which is also
   what Preview shows;
3. sends it over SSH and runs the documented `lpr` command. The temporary copy
   on the server is deleted as soon as `lpr` has taken it.

### Heavy pages

Exported decks (AI slide generators, Canva and the like) often put a 4K photo
behind every slide and draw glows with transparency. The printer has to
composite all of that at full resolution, which is why such jobs sit at
"Processing" for minutes. When a file is added, SoC Print looks for pages with
more than 12 MP of images, soft masks, transparency over large images, or
megabytes of vector drawing. With "Speed up heavy pages" on (the default), only
those printed pages are sent as one 300 dpi image each (grayscale for B/W
queues). Text-only pages are left untouched, and Preview shows exactly what
will be sent.

On a synthetic 10-slide deck of that kind (2 per sheet), this took the job from
18.7 MB to 2.8 MB, the PostScript a print server would make from it from 107 MB
to 3.6 MB, and Ghostscript's 600 dpi render time from 6.7 s to 0.4 s. Those are
proxies; the printer itself wasn't timed.

The queue panel is `lpq -P <queue>`, Cancel is `lprm`, and the quota panel is
`pusage`, with links to top up (socpay.comp.nus.edu.sg), prices and MySoC usage. The printer list comes from the DocHub print-queues page, including
the `-sx` (single-sided), `-dx` (double-sided colour) and `-nb` (no banner)
variants; any other queue can be typed in under "Another queue…".

It is a local site rather than a hosted one because it has to reach SoC's
servers from your network position and use your credentials, neither of which
should leave your computer.

## Staying signed in

- The password is stored in the operating system keyring (GNOME Keyring /
  KWallet, macOS Keychain, Windows Credential Manager), never in a file. Untick
  "Stay signed in" to keep it in memory only until SoC Print stops.
- One SSH connection is kept open and reused for every command, so the server
  sees one login, not one per print job. It reconnects by itself when needed.
- A password is offered once per connection attempt. If the server rejects a
  saved password (say you changed it), it is deleted and you are asked again;
  it is never retried, so a stale password can't lock the account.
- Sign out (click your username) closes the connection and deletes the saved
  password.

Connection route is automatic: direct to `stu` when it is reachable (SoC
network), otherwise through `stujump`. The jump host asks for the same password,
only accepts connections from inside NUS (use NUS VPN elsewhere), and on NUS
Wi-Fi also requires this device's SSH key to be registered with `skeys`; see
the DocHub "SSH Jump Host Guide".

## Security notes

- The server listens on 127.0.0.1 only. Every API call needs a per-run token
  that only the page itself knows, and the Host header is checked, so other
  websites open in your browser cannot make it print.
- ssh is given the password through a private unix socket by a helper
  (`SSH_ASKPASS`), so it never appears in a command line or environment
  variable.
- Unknown SSH host keys are accepted on first connection and pinned in
  `~/.ssh/known_hosts`; a changed key stops everything.

## Files

| Path | What it holds |
| --- | --- |
| `~/.config/soc-print/config.json` | Username, route and last-used print settings. `host`, `port`, `jump_host`, `jump_port` can be edited by hand. |
| `~/.cache/soc-print/` | Files being prepared (wiped on start and exit) and a private LibreOffice profile. |
| `$XDG_RUNTIME_DIR/soc-print/` | SSH control socket and the password helper's socket (removed on exit). |

`SOC_PRINT_SSH_ARGS` adds arguments to every `ssh` invocation, for example
`-o IdentityFile=~/.ssh/other_key`.

## Development

```
uv run pytest
```

The tests start `tests/fake_soc.py`, a real SSH server with fake `lpr`, `lpq`,
`lprm` and `pusage`, so sign-in, connection reuse, the jump-host route and
printing are exercised through the actual `ssh` client without touching SoC or
your keyring.

### Trying it without printing

Start the fake server, then run SoC Print against it with throwaway settings:

```
uv run python tests/fake_soc.py --port 2222 --state /tmp/fake-soc &

export XDG_CONFIG_HOME=/tmp/sp-demo/config XDG_CACHE_HOME=/tmp/sp-demo/cache
export XDG_DATA_HOME=/tmp/sp-demo/data XDG_RUNTIME_DIR=/tmp/sp-demo/run
mkdir -p $XDG_CONFIG_HOME/soc-print $XDG_RUNTIME_DIR
echo '{"host": "127.0.0.1", "port": 2222, "jump_host": "localhost", "jump_port": 2222}' \
  > $XDG_CONFIG_HOME/soc-print/config.json
export PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring
export SOC_PRINT_SSH_ARGS="-F /dev/null -o UserKnownHostsFile=/tmp/sp-demo/known_hosts -o PubkeyAuthentication=no"
uv run python -m socprint --port 6311
```

Sign in as `tester` with the password printed in `tests/conftest.py`. "Printed"
files land in `/tmp/fake-soc/jobs/`.
