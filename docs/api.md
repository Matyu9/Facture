# API

`/api/v1`, pour une appli de l'association (Adhérents, à terme) qui crée, émet et envoie ses factures et ses devis ici.

## Authentification

Un jeton par appli, créé par un administrateur dans **Paramètres → API** (ou `flask jeton-api --nom adherents [--emettre]`) et montré une seule fois :

```
Authorization: Bearer fct_…
```

Deux niveaux :

- **sans le droit « émettre »** : le jeton lit et crée des brouillons, qu'une personne vérifie et émet dans l'appli ;
- **avec le droit « émettre »** : il peut aussi émettre, envoyer par email, noter la réponse du client à un devis et en tirer la facture.

Les actions sont inscrites au journal au nom du jeton. 600 appels par minute au plus. Erreurs : `{"error": {"code", "message"}}` (401 jeton absent, 403 droit manquant, 404 pièce inconnue, 409 action impossible dans l'état de la pièce, 422 données refusées).

## Lire

- `GET /emetteurs` : codes, noms, régime de TVA.
- `GET /factures?client=<external_id>&source=…&reference=…&emetteur=<code>&type=facture|avoir&page=1&par_page=50` : factures et avoirs.
- `GET /devis?…` : les devis, mêmes filtres.
- `GET /factures/<id>`, `GET /devis/<id>` : pièce, lignes, totaux, `paid`, `balance`, `state`.
- `GET /factures/<id>/pdf`, `GET /devis/<id>/pdf` : le PDF archivé (409 pour un brouillon).

`state` d'une facture : `brouillon`, `a_payer`, `en_retard`, `partiel`, `payee`, `annulee`, `avoir`. D'un devis : `brouillon`, `en_attente`, `accepte`, `refuse`, `expire`, `facture`.

Un devis porte aussi `valid_until`, `decision` (`accepte`, `refuse` ou `null`) et `invoices` (la facture qui en est tirée) ; une facture tirée d'un devis porte `quote`.

## Créer une facture ou un devis

`POST /factures` ou `POST /devis`

```json
{
  "idempotency_key": "cotisation-2026-foyer-12",
  "issuer": "ecole",
  "customer": {
    "external_id": "foyer-12",
    "kind": "particulier",
    "name": "Famille Martin",
    "email": "martin@example.org",
    "address": "3 rue des Lilas",
    "postcode": "45000",
    "city": "Orléans"
  },
  "subject": "Cotisation 2026-2027",
  "external_id": "cotisation-12",
  "service_date": "2026-09-15",
  "lines": [
    {"label": "Adhésion", "unit_price": 3000},
    {"label": "Formation musicale", "quantity": "1", "unit_price": 21000, "description": "Élève : Léa"},
    {"label": "Réduction famille", "unit_price": -2000}
  ],
  "issue": false
}
```

- Montants en **centimes** (entiers), quantités et taux en texte décimal. `vat_rate` n'est lu que pour un émetteur assujetti.
- `customer`, au choix :
  - `{"id": 12}` : un client déjà enregistré dans l'appli ;
  - avec `external_id` : le client de l'appli appelante, retrouvé par `source` (par défaut le nom du jeton) et `external_id`, créé sinon, ses coordonnées mises à jour ;
  - sans `id` ni `external_id` : un **destinataire ponctuel**, imprimé sur la pièce sans créer de fiche client (`name` obligatoire ; l'adresse l'est pour émettre).
- `idempotency_key` : la même clé renvoie la même pièce (200) sans en créer d'autre. Création : 201. Une clé déjà utilisée pour une pièce de l'autre type : 409.
- `issue: true` (jeton qui peut émettre) émet aussitôt, à la date du jour ou à `issue_date` ; si la pièce ne peut pas l'être (adresse manquante…), rien n'est créé (422).
- Champs facultatifs : `service_end`, `customer_reference`, `notes` ; `due_date` pour une facture, `valid_until` pour un devis (sinon : délai de validité de l'émetteur). `service_date` est obligatoire pour émettre une facture (par défaut : le jour même), facultative sur un devis.

## Agir sur une pièce (jeton qui peut émettre)

Corps JSON facultatif.

- `POST /factures/<id>/emettre`, `POST /devis/<id>/emettre` — `{"issue_date": "2026-11-19"}` facultatif. Numéro, PDF archivé, pièce figée. Déjà émise : renvoyée telle quelle (200). Ce qui manque pour émettre : 422 avec le détail.
- `POST /factures/<id>/envoyer`, `POST /devis/<id>/envoyer` — `{"to", "subject", "body"}` facultatifs. Envoie la pièce émise par email, PDF joint, à l'adresse du client (ou à `to`), avec le texte de l'émetteur (ou `subject` / `body`). 409 pour un brouillon, 503 si l'envoi d'emails n'est pas configuré.
- `POST /devis/<id>/accepter`, `POST /devis/<id>/refuser` : note la réponse du client (409 si le devis est déjà facturé).
- `POST /devis/<id>/facturer` — `{"issue": true, "issue_date", "service_date", "service_end"}` facultatifs. Crée la facture du devis (mêmes destinataire, objet et lignes ; le devis passe « accepté »), en brouillon ou émise. Rejouée, renvoie la facture déjà tirée du devis (200).

## Exemple : facturer une cotisation de bout en bout

```sh
curl -X POST https://facture.example.org/api/v1/factures \
  -H "Authorization: Bearer $JETON" -H "Content-Type: application/json" \
  -d '{"idempotency_key": "cotisation-2026-12", "issuer": "ecole", "issue": true,
       "customer": {"external_id": "foyer-12", "name": "Famille Martin", "email": "martin@example.org",
                    "address": "3 rue des Lilas", "postcode": "45000", "city": "Orléans"},
       "lines": [{"label": "Cotisation 2026-2027", "unit_price": 3000}]}'
# → 201 {"id": 42, "number": "FA-2026-0007", "pdf_url": "/api/v1/factures/42/pdf", …}

curl -X POST https://facture.example.org/api/v1/factures/42/envoyer -H "Authorization: Bearer $JETON"
```
