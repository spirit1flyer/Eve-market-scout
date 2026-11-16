# ESI data usage and authorization

EVE Market Scout is an independent, open-source desktop application. It uses
CCP's EVE Swagger Interface (ESI) and EVE Online SSO. It is not affiliated with
or endorsed by CCP Games.

The application is provided for use at your own risk. ESI data can be delayed,
incomplete, unavailable, or inaccurate, and calculations may contain errors.
Verify important market or industry decisions in EVE Online before acting.

## What the application reads

The trading login requests read-only access for:

- character wallet balance, transactions, and journal;
- active and historical character market orders;
- character skills and NPC standings used for fee calculations;
- player-structure metadata and structure-market orders; and
- structure search used by the add-station workflow.

The separate Industry character roster requests read-only access for:

- industry-relevant skills;
- NPC standings;
- owned blueprints; and
- player-structure metadata for blueprint locations.

The application also reads public ESI resources such as regional market
orders, market history, routes, stations, systems, and type names. Public
resources do not require a character login.

Authentication uses the official EVE Online SSO page. EVE Market Scout never
receives or stores your EVE Online password; that password should only be
entered on the official EVE Online sign-in page.

## When data is acquired

- Scanner orders use complete shared snapshots until ESI's saved expiry,
  normally about five minutes. Each selected hub is evaluated independently.
- Jita used as another hub's price reference is preserved across restarts and
  may be up to 24 hours old. Direct Jita scans and explicit refreshes use
  scanner freshness.
- Stock Market checks its selected hub on load/open and reuses orders acquired
  less than one hour ago, including newer scanner acquisitions. The clock
  measures acquisition age, not the time a tab was opened. Industry may reuse
  older saved orders; no new scanner-frequency Industry schedule is imposed.
- Trade synchronization retains its 20-minute scheduling floor. Wallet
  resources keep their separate saved absolute ESI expiry clocks.
- Holdings history checks tracked items on hub use, preferring reuse for one
  hour and always respecting later ESI expiry. Recent ESI rows and their
  original freshness metadata are saved separately from imported archives.
  Failed fetches or saves do not claim durable successful completion.
- Skills retain their one-hour preference. Saved reputation stays readable;
  ordinary acquisition paths refresh it when seven days old, and explicit
  reputation controls request an eligible update. Owned blueprint acquisition
  follows the existing user-triggered workflow.
- Shared providers reuse unexpired responses for covered public and
  authorization-scoped private resources. Manual refresh requests an eligible
  update; it does not override saved server expiry. An unexercised route is
  not evidence of successful runtime coverage.
- Trading and Industry share an SSO-specific Retry-After cooldown. Failed
  grants receive a short retry delay without blocking unrelated valid grants;
  server throttling covers both token and verify calls.

Requests identify the application with a contactable User-Agent, send an
explicit `X-Compatibility-Date`, and use a process-wide ESI rate gate.
Paginated snapshots are validated before acceptance. SSO has its own cooldown.
Implementation and runtime verification status are recorded separately in
`ESI_ACQUISITION_GOALS.md` and `ESI_DIAGNOSTICS.md` in the source distribution.

## Local storage

Access and refresh tokens are stored only in the application's local data
directory (`%APPDATA%\EVEMarketScout` on Windows). Character-derived caches,
settings, tracking data, recent public-history snapshots, and credential-free
SSO cooldown deadlines are stored there as well. The application does not
operate an external account service, telemetry service, or automatic crash
uploader, and it does not send character data back to the project author.

Tokens are not separately encrypted by the application. They rely on the
normal access controls of your Windows user profile, so anyone with access to
that profile may be able to read them.

Treat local token and character-data files as sensitive. Do not publish or
attach them to bug reports.

## Removing access

You can remove a trading or Industry character from the application and revoke
the application's authorization from EVE Online account management. Deleting
the corresponding local authorization file removes the locally stored tokens;
revoking access through EVE Online invalidates the grant at CCP.

© 2014 CCP hf. All rights reserved. "EVE", "EVE Online", "CCP", and all related
logos and images are trademarks or registered trademarks of CCP hf.
