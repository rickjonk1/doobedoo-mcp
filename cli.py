"""Beheeropdrachten voor op de server (via de AWS-console)."""
import click


def registreer_cli(app):
    @app.cli.command("uitnodiging")
    @click.argument("email")
    @click.option("--beheerder", is_flag=True, help="Maak er een beheerder van.")
    def uitnodiging(email, beheerder):
        """Maak (of herstel) een gebruiker en toon een eenmalige link."""
        from beheer import _onbruikbaar_wachtwoord, maak_uitnodiging
        from database import db
        from models.user import User
        email = email.strip().lower()
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(email=email, name=email, password_hash=_onbruikbaar_wachtwoord(),
                        role="admin" if beheerder else "gebruiker", totp_enabled=False)
            db.session.add(user)
        elif beheerder:
            user.role = "admin"
        user.active = True
        db.session.commit()
        click.echo(maak_uitnodiging(user))

    @app.cli.command("opschonen")
    def opschonen():
        """Verwijder verlopen tokens, codes en logregels ouder dan 180 dagen."""
        from datetime import timedelta

        from database import db
        from models.partner_client import OAuthAuthCode, PartnerApiLog, PartnerToken
        from utils import nl_now
        nu = nl_now()
        a = OAuthAuthCode.query.filter(OAuthAuthCode.expires_at < nu).delete()
        t = PartnerToken.query.filter(
            (PartnerToken.refresh_expires < nu) |
            ((PartnerToken.refresh_expires.is_(None)) & (PartnerToken.expires_at < nu))).delete()
        log = PartnerApiLog.query.filter(PartnerApiLog.ts < nu - timedelta(days=180)).delete()
        db.session.commit()
        click.echo(f"Opgeschoond: {a} codes, {t} tokens, {log} logregels.")
