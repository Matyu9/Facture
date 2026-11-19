# API

`/api/v1`, pour une appli de l'association (Adhérents, à terme) qui fait ses factures ici.

## Authentification

Un jeton par appli, créé par un administrateur dans **Paramètres → API** (ou `flask jeton-api --nom adherents [--emettre]`) et montré une seule fois :

```
Authorization: Bearer fct_…
```

Sans le droit « émettre », le jeton ne crée que des brouillons, qu'une personne vérifie et émet. Les actions sont inscrites au journal au nom du jeton. 600 appels par minute au plus. Erreurs : `{"error": {"code", "message"}}`.

## Lire

- `GET /emetteurs` : codes, noms, régime de TVA.
- `GET /factures?client=<external_id>&source=…&reference=…&emetteur=<code>&page=1&par_page=50`
- `GET /factures/<id>` : pièce, lignes, totaux, `paid`, `balance`, `state` (`brouillon`, `a_payer`, `en_retard`, `partiel`, `payee`, `annulee`, `avoir`).
- `GET /factures/<id>/pdf` : le PDF archivé (409 pour un brouillon).

Une pièce saisie dans l'appli pour un destinataire ponctuel (sans fiche client) a un `customer` sans `id`, `source` ni `external_id` : seulement ses coordonnées.

## Créer

`POST /factures`

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
- Le client est retrouvé par `source` (par défaut le nom du jeton) et `external_id`, créé sinon, et ses coordonnées sont mises à jour.
- `idempotency_key` : la même clé renvoie la même pièce (200) sans en créer d'autre. Création : 201.
- `issue: true` (jeton qui peut émettre) émet aussitôt ; si la pièce ne peut pas l'être (adresse manquante…), rien n'est créé (422).
- Champs facultatifs : `service_end`, `due_date`, `customer_reference`, `notes`.
