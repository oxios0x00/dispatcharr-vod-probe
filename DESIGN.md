# vod-probe : note de conception (2026-09-21)

Plugin Dispatcharr indépendant qui sonde les relations VOD et écrit le résultat dans le catalogue de Dispatcharr, pour que tous les outils qui lisent son API puissent s'en servir. Projet séparé de vod-manager et de Strmarr, à décider plus tard. Chaque fait est marqué **[vérifié]** (lu dans le code de Dispatcharr ou du plugin, ou testé sur mon installation) ou **[non vérifié]**.

## Objectif

Mesurer une seule fois, au même endroit, la qualité réelle de chaque relation VOD (film ou épisode) et la rendre lisible par tous : Strmarr, vod-manager, d'autres plugins, des services extérieurs via l'API. Ce n'est pas une question de vitesse des échanges : c'est éviter que chacun sonde de son côté.

Périmètre :
- Ce que le plugin fait : sonder les relations nouvelles ou changées, écrire `quality`, `resolution` et un bloc `probe` dans les `custom_properties` de la relation.
- Ce qu'il ne fait pas : supprimer ou fusionner des relations, renommer des titres, écrire des `.strm`, choisir une version gagnante. Ce sont les rôles de vod-manager et de Strmarr.

## Pourquoi

- Dispatcharr ne sonde jamais rien : aucun `ffprobe` ni `ffmpeg` dans son code VOD ni dans son proxy VOD **[vérifié]**.
- `quality_info` est calculé à la volée à partir de `custom_properties["quality"]` ou `["resolution"]` de la relation, sinon d'un mot-clé de qualité dans le nom du titre. Les branches « vidéo » et « débit » sont du code mort pour les films (aucun champ `video` ni `bitrate` dans les modèles de ta version) **[vérifié]**.
- Chez moi, `quality_info` vaut `null` partout : les relations n'ont que `basic_data` et `detailed_fetched`, et les noms ont été nettoyés par vod-manager **[vérifié]**.
- `basic_data` (l'entrée brute du fournisseur) ne contient ni qualité, ni résolution, ni débit **[vérifié]**. Ce qu'une fiche détaillée peut contenir dépend du fournisseur **[non vérifié pour strong8k]**.
- Les versions d'un titre sont des relations du même compte dans des catégories différentes (par exemple « … MOVIES » et « … MOVIES 4K 3840P Dolby Vision ») **[vérifié sur 3 titres]**.

## Faits techniques sur lesquels le plugin s'appuie

- Un plugin tourne dans le processus de Dispatcharr et accède à sa base avec l'ORM Django ; vod-manager le fait déjà (modèles `M3UMovieRelation`, `M3UEpisodeRelation`) **[vérifié]**.
- Les relations sont en lecture seule dans l'API REST : seul un plugin peut les modifier **[vérifié]**.
- L'API `providers` renvoie `custom_properties` de chaque relation, donc tout ce qui y est écrit est lisible par les clients **[vérifié]**.
- La synchronisation de liste d'une relation existante fait `{**existant, 'basic_data': …}` : les clés ajoutées survivent **[vérifié pour les films, lu dans le code ; le commentaire des séries dit la même chose, code non relu]**.
- Une relation nouvelle est créée avec seulement `basic_data` et `detailed_fetched: False` : une relation recréée par un réimport perd ses données de sonde **[vérifié]**.
- Le sondage de vod-manager pointe `ffprobe` sur l'URL servie par Dispatcharr, qui suit la chaîne de redirections jusqu'au fournisseur ; seuls quelques Mo sont lus, avec délai de 25 s (`probe.py`) **[vérifié dans le code du plugin]**. Le proxy a un mode « Redirect » qui ne réserve pas de connexion après le choix de l'URL (docstring de `_select_vod_stream`) : à confirmer si le sondage l'emprunte **[non vérifié]**.
- Un compte a `max_streams` à 0 chez moi ; la signification exacte (illimité) n'est pas vérifiée **[non vérifié]**.

## Contrat de données (proposition, à figer avant d'écrire du code)

Dans `custom_properties` de la relation :

- `quality` : texte libre lu tel quel par `quality_info`. Utiliser le vocabulaire de Dispatcharr lui-même : `4K`, `1080p`, `720p`, `480p` (sinon `SD` ou `LxH`). Ainsi tout client qui lit déjà `quality_info` s'en sert sans rien changer.
- `resolution` : `LxH` (par exemple `3840x1608`), utile seul si `quality` est absent.
- `probe` : bloc détaillé, pour ceux qui veulent plus :
  - `schema_version` : version du format (comme `PROBE_SCHEMA_VERSION` de vod-manager, actuellement 3).
  - `probed_at` : date du sondage (UTC).
  - `status` : `ok`, `error`, `unreachable` (avec `error` court et nombre de tentatives).
  - `tier` : palier précis (`2160p`, `1080p`, `720p`, `480p`, `sd`, `unknown`), calculé avec la règle largeur ou hauteur de `classify_quality`.
  - `hdr` : `sdr`, `hdr10`, `hlg`, `dolby_vision` (comme `classify_hdr`).
  - `video` : codec, profil, profondeur de bits, débit (`bit_rate` ou étiquette `BPS`), fréquence d'images.
  - `audio` : liste des pistes (codec, canaux, langue, indicateur d'audiodescription).
  - `subtitles` : liste des langues et formats.
  - `duration_secs`, `container`.
  - `source` : `plugin=vod-probe`, version du plugin.

Règles d'écriture :
- Lire la relation, fusionner ses clés dans le dictionnaire existant, puis `save(update_fields=["custom_properties"])`. Ne jamais toucher à `basic_data`, `detailed_info` ni aux drapeaux de Dispatcharr.
- Ne jamais écrire `quality`/`resolution` sur un sondage en échec : garder la dernière valeur connue et marquer `probe.status`.
- Risque : la synchronisation de liste lit puis réécrit le dictionnaire de la relation ; une écriture du plugin entre sa lecture et son écriture peut être perdue. Faible probabilité, à traiter (écriture atomique au niveau de la clé, ou vérification après chaque rafraîchissement) **[non vérifié : moteur de base de données à confirmer]**.

## Fonctionnement

1. **Sélection des relations à sonder :** relations actives (comptes actifs, catégories activées) sans bloc `probe`, ou avec un `schema_version` périmé, ou dont le sondage précédent a échoué depuis assez longtemps.
2. **Séries :** un épisode par saison, comme vod-manager. Question ouverte : écrire le résultat seulement sur l'épisode sondé, ou aussi sur les autres épisodes de la saison avec une marque `inferred_from` (un épisode sondé ne garantit pas la qualité des autres).
3. **Rattrapage initial :** tout l'existant est nouveau (environ 1 209 films avec 1 à 2 relations, plus les séries), donc c'est un passage long, en tâche de fond, avec plafond de concurrence.
4. **Régime normal :** seulement les nouveautés et les changements détectés.
5. **Re-sondage lent en rotation :** quelques relations par jour, les plus anciennes d'abord, pour le cas invisible (un fournisseur qui remplace un fichier en gardant le même `stream_id`). Action manuelle en plus pour re-sonder une relation précise.
6. **Détection de changement :** signal fiable à identifier dans une relation (`updated_at`, `last_seen`, `last_advanced_refresh`) **[non vérifié]**.

## Contrôle de charge

- Plafond de sondages simultanés, et au plus un sondage à la fois par compte fournisseur au départ (limites de connexion).
- Délai par sondage de 25 s (comme aujourd'hui), avec repli et nombre maximal de tentatives par relation pour ne pas boucler sur un titre cassé.
- Fenêtre horaire réglable, et pause si une lecture est en cours **[non vérifié : comment savoir depuis un plugin qu'une lecture est active]**.
- Observation chez moi : passer de 1 à 2 sondages en parallèle n'a pas accéléré, goulot non expliqué. Une mesure était prévue, à faire avant de fixer les valeurs par défaut.

## Interface du plugin (comme vod-manager)

- Réglages : plafond de concurrence, fenêtre horaire, cadence de rotation, délai de sondage, mode Dry run.
- Actions : Scan (liste des relations à sonder), Process (sonder), Re-sonder une relation (par `stream_id`), Réessayer les erreurs, Statistiques de couverture (pourcentage de relations sondées, par type), Effacer les données du plugin (retire uniquement les clés écrites par lui).
- Notifications de fin de passage, comme vod-manager.

## Réutilisation du code de vod-manager

Copier, ne pas importer :
- `probe.py` (198 lignes) : appel de `ffprobe`, paliers de qualité, HDR, débit.
- `probe_summary.py` (91 lignes) : résumé compact du résultat (le bloc `probe` en découle).
- Du `store.py` (716 lignes), seulement ce qui sert : ici les résultats vivent dans le catalogue de Dispatcharr, donc plus besoin d'un cache SQLite pour eux. Un état minimal (file d'attente, verrous, historique de passages) peut suffire, à décider.

## Relations avec les autres projets

- **Strmarr :** lit `quality_info` en priorité s'il est renseigné et récent, sinon sonde lui-même pour les titres voulus. Le plugin est une accélération facultative, pas une dépendance.
- **vod-manager :** aujourd'hui il sonde et écrit dans son propre cache. Il pourrait lire les résultats de vod-probe au lieu de sonder, ce qui éviterait le double sondage. C'est une réorganisation de ton plugin actuel, à décider séparément.
- Les deux plugins ne doivent pas sonder les mêmes relations en même temps (limites de connexion par compte).

## Risques et questions ouvertes

1. Signal de changement fiable dans une relation, et point d'accroche après un rafraîchissement du VOD (le plugin actuel passe par une tâche planifiée).
2. Perte d'une écriture face à la synchronisation de liste (voir plus haut).
3. Une relation supprimée puis recréée perd son sondage : accepté, elle sera resondée.
4. Format de `quality` non normalisé par Dispatcharr : s'aligner sur son vocabulaire, le documenter, et prévoir de rester compatible si Dispatcharr ajoute un jour ses propres champs.
5. Épisodes : écrire le résultat sur les autres épisodes de la saison ou non.
6. Connaître l'effet du sondage sur les limites de connexion du fournisseur (tu m'as dit que la limite d'une connexion n'est pas vraiment appliquée chez toi ; ce n'est pas garanti ailleurs).
7. Sonder par l'URL du proxy de Dispatcharr ou directement chez le fournisseur : le plugin actuel passe par l'URL servie par Dispatcharr.

## Étapes proposées

1. Lire dans le code de Dispatcharr le système de plugins (déclencheurs, tâches, verrous, notifications) et le signal de changement d'une relation.
2. Fixer le contrat de données ci-dessus.
3. Mesurer le goulot de sondage en parallèle (1 contre 2 contre 4), en lecture seule.
4. Prototype en Dry run : lister les relations à sonder et estimer la durée, sans écrire.
5. Première écriture réelle sur un petit nombre de relations, avec ton accord explicite : je n'ai pas d'environnement de test, donc rien ne s'écrit dans ton Dispatcharr de production sans validation.
