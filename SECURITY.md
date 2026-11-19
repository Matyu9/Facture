# Sécurité

Facture contient les coordonnées des clients de l'association (dont des familles d'adhérents) et des pièces comptables qui doivent rester intactes. Une faille y est donc prise au sérieux.

## Signaler une faille

**N'ouvrez pas d'issue publique.** Passez par le signalement privé de GitHub : onglet **Security** du dépôt, puis **Report a vulnerability**. Seuls les mainteneurs voient le signalement.

Indiquez si possible ce que la faille permet, les étapes pour la reproduire, et la version ou le commit concerné. N'utilisez jamais de vraies données pour une démonstration.

## Ce qui est concerné

- La connexion (mot de passe, double authentification, fournisseur OIDC), les sessions et les liens à usage unique.
- Les droits : qui voit, prépare, émet les factures et enregistre les règlements.
- L'intégrité des pièces émises : numérotation sans trou, PDF archivé et son empreinte.
- L'API et ses jetons.
