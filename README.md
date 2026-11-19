# Facture

La facturation d'une association : devis, factures et avoirs, règlements, PDF archivés, et une API pour que ses autres applis (Adhérents) fassent leurs factures ici.

Elle complète Adhérents (annuaire, cotisations, comptes) et l'Inventaire (matériel prêté). Même socle, même interface : Flask, PostgreSQL, Jinja, HTMX et Pico, sans étape de build.

```
  Adhérents ──── connexion (OIDC) ────▶ Facture ◀──── API (jeton) ──── Adhérents, à terme
                                          │
                       factures, avoirs, règlements, PDF archivés
```

## Ce que fait l'appli

- **Émetteurs** : l'association, ou chacune de ses régies. Chacun a sa dénomination, son SIRET, son RNA, son régime de TVA (franchise, exonéré, assujetti), sa mention de TVA, sa numérotation, ses coordonnées bancaires, son email d'envoi, et la mise en page de ses factures (disposition, couleur, logo, textes d'introduction et de conclusion), avec aperçu.
- **Clients** : structures (mairies, comités, entreprises) et particuliers (familles). Un client venu d'Adhérents garde sa référence et est tenu à jour à chaque facture. Une facture peut aussi s'adresser à un destinataire ponctuel, saisi sur la pièce sans créer de fiche (et enregistré plus tard si besoin).
- **Articles** : les prestations qui reviennent (concert, animation, location…), pour remplir une ligne en un clic.
- **Devis** : même saisie que les factures, numérotés dans leur propre série, avec une date de validité et un cadre « Bon pour accord » sur le PDF. Envoi par email, réponse du client (accepté, refusé, expiré), puis transformation en facture d'un clic.
- **Factures** : brouillon modifiable, aperçu PDF marqué « BROUILLON », puis émission. L'émission donne le numéro suivant (`FA-2026-0001`, par émetteur, type de pièce et année, sans trou), fige l'émetteur et le client tels qu'ils sont ce jour-là et archive le PDF avec son empreinte. Une pièce émise ne se modifie ni ne se supprime.
- **Avoirs** : total ou partiel, ils corrigent une facture émise. Leur propre série de numéros.
- **Règlements** : virement, chèque, espèces, carte, prélèvement, paiement en ligne, chèques vacances. Reste à payer, factures en retard signalées une fois aux personnes qui suivent les règlements.
- **Envoi** par email avec le PDF archivé en pièce jointe, et relance.
- **Journal des ventes** en CSV (une ligne par taux de TVA, avoirs en négatif), pour le trésorier ou le comptable.
- **API** (`/api/v1`) : une appli crée des factures et des devis (brouillons, ou émis si son jeton le permet), les émet, les envoie par email, note la réponse à un devis et en tire la facture, et lit leurs états et leurs PDF. Voir [docs/api.md](docs/api.md).
- **Connexion** par mot de passe (avec double authentification) ou par Adhérents (OIDC), avec le rôle déduit des groupes. Rôles et droits réglables, journal de toutes les actions.

## Rôles

| | Administrateur | Gestionnaire | Lecteur |
|---|:---:|:---:|:---:|
| Consulter factures, avoirs, règlements, clients | ✅ | ✅ | ✅ |
| Préparer les brouillons, clients, articles | ✅ | ✅ | — |
| Émettre, envoyer, faire un avoir | ✅ | ✅ | — |
| Enregistrer les règlements | ✅ | ✅ | — |
| Émetteurs, comptes, rôles, jetons d'API, paramètres | ✅ | — | — |

## Démarrer en développement

Python 3.12 ou plus récent et PostgreSQL 16 ou plus récent ; ni Docker, ni bibliothèque système (les PDF sont produits en Python pur, avec les polices DejaVu fournies dans `app/fonts`).

```sh
createdb facture && createdb facture_test
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
export FLASK_APP=wsgi.py FLASK_DEBUG=1
.venv/bin/flask db upgrade
.venv/bin/flask create-admin
.venv/bin/flask run --port 8002
```

L'appli répond sur <http://127.0.0.1:8002> (Adhérents est sur 8001, l'Inventaire sur 8000). Premier pas : **Émetteurs → Nouvel émetteur**.

## Tests

```sh
.venv/bin/python -m pytest -m "not e2e" -n auto   # sans navigateur
.venv/bin/python -m pytest tests/test_e2e.py       # dans Chromium (playwright install chromium, une fois)
.venv/bin/ruff check . && .venv/bin/ruff format .
```

Ils tournent sur une vraie base PostgreSQL (`facture_test`) : toutes les routes avec tous les rôles, l'exploration de tous les liens, les calculs et la numérotation (verrou compris), le contenu des PDF, l'API, la connexion OIDC avec un faux fournisseur qui signe de vrais jetons, les migrations, et des parcours dans un vrai navigateur sous la politique de sécurité du contenu.

## Mise en service

```sh
cp .env.example .env    # SECRET_KEY, POSTGRES_PASSWORD, BASE_URL, et OIDC_* pour la connexion par Adhérents
mkdir -p data && sudo chown 1000:1000 data
docker compose up -d --build
docker compose exec app flask create-admin
```

L'appli écoute sur `127.0.0.1:8002`, derrière un reverse proxy HTTPS. **Sauvegardez la base et le dossier `data/factures` ensemble** : ce sont les originaux des pièces émises, à conserver dix ans. `flask verifier-archive` relit chaque PDF et vérifie son empreinte.

Pour brancher la connexion sur Adhérents : y déclarer l'appli (adresses dans `.env.example`), ouvrir l'accès à un groupe, puis renseigner `OIDC_*`. Les groupes qui donnent chaque rôle se règlent dans **Rôles**.

## Ce qui n'est pas fait

- **Devis** (et leur transformation en facture).
- **Facturation électronique** : les associations assujetties à la TVA devront émettre des factures électroniques (Factur-X, via une plateforme agréée) à partir de septembre 2027. L'appli produit des PDF simples ; à prévoir avant cette date si un émetteur est assujetti.
- **Côté Adhérents** : l'appel à l'API depuis la phase « Règlements ».
- Factures récurrentes, rapprochement bancaire, relances automatiques par email.

## Organisation du code

```
app/
├── blueprints/   Routes : invoices, customers, products, issuers, api, api_tokens, et le socle (comptes, rôles…)
├── services/     billing (calculs, émission, avoirs, règlements), pdf, archive, money, oidc, api_tokens…
├── fonts/        DejaVu Sans Condensed (licence libre), pour les PDF
├── templates/    Jinja
└── static/       CSS, JavaScript, Pico et HTMX
migrations/       Alembic
tests/            pytest et Playwright
```

Le socle (comptes, double authentification, rôles, journal, CSP, interface) est copié d'Adhérents, et le client OIDC de l'Inventaire ; les applis évoluent séparément.
