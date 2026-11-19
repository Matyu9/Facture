# Journal des versions

## 0.2.0 — 19 novembre 2026

- Destinataire ponctuel : une facture peut s'adresser à quelqu'un qui n'est pas dans les clients, saisi directement sur la pièce. On peut l'enregistrer comme client tout de suite, ou plus tard depuis la fiche de la facture.
- Mise en page des factures, par émetteur (Émetteurs → fiche → Mise en page) : disposition (classique, bandeau de couleur, sobre), couleur, taille et position du logo, textes d'introduction et de conclusion, intitulés. Aperçu PDF avant d'enregistrer. Une pièce émise garde l'apparence qu'elle avait.
- Logo d'émetteur conservé en haute définition (1200 px) pour l'impression ; sur fond blanc en disposition bandeau.

## 0.1.0 — 19 novembre 2026

Première version.

- Socle repris d'Adhérents (comptes, double authentification, rôles, journal, notifications, apparence, CSP stricte) et client OIDC de l'Inventaire (rôle par groupes, déconnexion par le fournisseur et vers lui).
- Émetteurs avec régime de TVA, mentions légales, numérotation et coordonnées bancaires.
- Clients, articles, factures et avoirs : brouillon, émission numérotée sans trou, copie figée, PDF archivé avec empreinte.
- Règlements, retards signalés, envoi et relance par email, journal des ventes en CSV.
- API par jeton pour créer et lire des factures.
