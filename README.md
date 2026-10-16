# doobedoo MCP

De MCP-server van doobedoo op **https://mcp.doobedoo.nl**: Claude (of ChatGPT) werkt via
deze server rechtstreeks in de Odoo-databases van doobedoo. Los van Hein Koning:
eigen AWS-account, eigen database, eigen sleutels.

## Wat zit erin
- **Inloggen**: wachtwoord (minstens 12 tekens, bcrypt) en verplichte tweestapsverificatie (TOTP).
- **Beheer** (`beheer.py`): Odoo-databases registreren (API-sleutel Fernet-versleuteld),
  gebruikers uitnodigen via een eenmalige link, koppelingen intrekken, logboek.
- **OAuth 2.1** (`partner_api/oauth2.py`): dynamische clientregistratie, autorisatiecode + PKCE,
  tokens alleen als hash opgeslagen, toegang 1 uur, verversen 30 dagen.
- **MCP** (`mcp_server/`): Streamable HTTP op `/mcp`. Odoo-tools (zoeken, lezen, aanmaken,
  wijzigen), documenten lezen, inkoopfacturen. Geen AccountView.

## Beveiliging
- Server: EC2 t4g.micro in een eigen VPC, alleen poort 80/443 open, **geen SSH**
  (beheer via AWS Session Manager), IMDSv2 verplicht.
- Schijf versleuteld (EBS, KMS); dagelijkse snapshots, 14 dagen bewaard.
- Geheimen (`SECRET_KEY`, `FLASK_SECRET`) worden op de server zelf aangemaakt en in
  Parameter Store (SecureString) bewaard. Ze komen nooit in GitHub of in deploylogs.
- GitHub rolt uit via OIDC (geen AWS-sleutels in GitHub), alleen vanaf `main`.
- Container: alleen-lezen bestandssysteem, geen Linux-capabilities, niet als root.
- Browser: strikte CSP, HSTS, CSRF-tokens, `__Host-`-sessiecookie, geen externe scripts of fonts.
- Odoo-adressen alleen via https en nooit naar interne netwerken.

## Uitrollen
Een push naar `main` draait de tests, werkt de infrastructuur bij (`infra/stack.yml`),
bouwt een arm64-image naar ECR en rolt dat via SSM uit op de server (`deploy/`).

## Eerste beheerder
Bij de eerste start maakt de server een beheerder voor `info@doobedoo.nl` aan en zet een
eenmalige link in het serverlogboek. Ophalen via AWS Session Manager:

```
sudo docker logs app 2>&1 | grep "instellen via"
```

Of maak een nieuwe link:

```
sudo docker exec app flask --app app uitnodiging naam@doobedoo.nl --beheerder
```

## Lokaal draaien
```
pip install -r requirements.txt
APP_ENV=development flask --app app run
python -m pytest -q tests
```
