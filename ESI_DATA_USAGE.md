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

- Scanner regional orders follow ESI's server-provided cache expiry, normally
  about five minutes.
- Trade synchronization has a 20-minute scheduling floor, while each wallet
  endpoint keeps its own ESI expiry clock.
- Stock Market holdings history is refreshed at most once per hour per hub.
- Character skills, standings, and owned blueprints are fetched only after a
  corresponding user action.
- Manual refresh actions reuse an unexpired ESI representation rather than
  bypassing CCP's cache headers.

Requests identify the application with a contactable User-Agent, send an
explicit `X-Compatibility-Date`, honor `Expires`/cache headers, validate
paginated `Last-Modified` values, and share one process-wide rate-limit gate.

## Local storage

Access and refresh tokens are stored only in the application's local data
directory (`%APPDATA%\EVEMarketScout` on Windows). Character-derived caches,
settings, and tracking data are stored there as well. The application does not
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
