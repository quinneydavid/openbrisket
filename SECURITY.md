# Security

OpenBrisket is made to run at home, behind your own network.

- Everything under `/api` and `/mcp` needs a token. The main access token has full rights; mint tokens with
  fewer rights for agents and screens on the Settings page, and revoke them there.
- The main access token can be replaced on the Settings page. Do that if it has ever been shown somewhere it
  should not have been.
- Wrong tokens are counted, and past ten a minute they are refused unread.
- OpenBrisket itself serves plain HTTP. Put it behind a reverse proxy with HTTPS before reaching it from outside
  your home network. The page warns you when it is not on HTTPS.
- The settings file holds your Home Assistant token and is created readable by you only. Give OpenBrisket a
  Home Assistant user that can only read.
- The reader firmware never writes to the thermometer or the fan controller.

## Reporting a problem

Please report a vulnerability privately through GitHub's "Report a vulnerability" button on the Security tab of
this repository, not in a public issue.
