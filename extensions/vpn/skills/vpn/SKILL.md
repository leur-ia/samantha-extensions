---
name: vpn
description: The user's VPNs (WireGuard, OpenVPN, Proton…): am I connected, connect, disconnect.
---
# VPN

Tools (`name` is a profile name fragment; omitted, the only one that fits):
- `vpn.status {}` → `{connected, vpns: [{name, type, active, address?}]}`.
- `vpn.connect {name?}`, `vpn.disconnect {name?}`.

Answering:
- "Active le VPN": connect, then "Connecté à <name>."; several profiles and none named: ask which.
- A profile needing an unsaved password: pass on the tool's explanation.
- No VPN configured: say they're added once in the network settings (or a WireGuard .conf imported there).
