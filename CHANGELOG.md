# Journal des versions

## 0.4.0 — 19 novembre 2026

- Numérotation réglable par émetteur et par série (Émetteurs → fiche → Modifier la numérotation) : un format libre avec des balises, `FA-{AAAA}-{NNNN}` → FA-2026-0001. Année sur 4 ou 2 chiffres, mois, jour, ou rien ; compteur en chiffres (complété de zéros ou non) ou en lettres (A, B… Z, AA…). Le compteur repart à 1 chaque mois, chaque année ou jamais selon les balises du format. Exemple mis à jour pendant la saisie.
- Séries de factures : un émetteur peut avoir plusieurs séries, chacune avec ses numéros continus (par exemple « Générale » `FA-2026-0001` et « Comités d'entreprise » `CE-2026-0001`). On choisit la série sur la facture, les avoirs suivent celle de leur facture, et l'ordre des dates est vérifié série par série. Filtre « Série » dans la liste des factures et colonne « Série » dans le journal des ventes. API : `"series": "ce"` à la création ou en tirant une facture d'un devis, `?serie=` pour lister, séries décrites dans `GET /emetteurs`.
- Numéro de départ : pour continuer une série commencée dans un autre outil. Il ne peut pas redescendre sous un numéro déjà donné.
- Les numéros restent uniques dans toute l'appli : deux séries de même forme sont refusées, un nouvel émetteur dont les formats par défaut sont pris reçoit son code en tête, et une émission qui retomberait sur un numéro existant est refusée.
- Migration : les préfixes et nombres de chiffres d'avant deviennent des formats équivalents. Le préfixe `DE-` des devis, commun à tous les émetteurs en 0.3.0, prend le code de l'émetteur en tête pour le deuxième et les suivants.

## 0.3.0 — 19 novembre 2026

- Devis : brouillon, émission numérotée dans leur propre série (`DE-2026-0001`), PDF avec validité et cadre « Bon pour accord », envoi par email, réponse du client (accepté, refusé, expiré), et transformation en facture d'un clic. Ils restent hors du journal des ventes et des encaissements.
- API : création de devis, émission et envoi par email d'une pièce existante, réponse du client et facture tirée d'un devis ; destinataire ponctuel ou client déjà enregistré (`customer.id`).
- Mise en page : le logo s'envoie avec le reste du formulaire. Avant, l'envoyer rechargeait la page et perdait les réglages pas encore enregistrés.

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
