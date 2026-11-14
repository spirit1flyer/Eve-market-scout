# ESI data usage and authorization

EVE Market Scout is an independent, open-source desktop application. It uses
CCP's EVE Swagger Interface (ESI) and EVE Online SSO. It is not affiliated with
or endorsed by CCP Games.

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
operate an external account service and does not upload this data to the
project author.

Treat local token and character-data files as sensitive. Do not publish or
attach them to bug reports.

## Removing access

You can remove a trading or Industry character from the application and revoke
the application's authorization from EVE Online account management. Deleting
the corresponding local authorization file removes the locally stored tokens;
revoking access through EVE Online invalidates the grant at CCP.

EVE Online and the EVE logo are registered trademarks of CCP hf. All EVE data
and imagery are property of CCP hf.
