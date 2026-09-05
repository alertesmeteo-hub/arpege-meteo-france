# ARPEGE Météo-France France — cartes et prévisions WordPress

Ce dépôt construit une chaîne directe **Météo-France ARPEGE → GitHub → WordPress/Avada**. Il publie les cartes interactives et les prévisions horaires de 34 746 communes françaises sur une branche `data`, sans Open-Meteo ni autre intermédiaire météorologique.

## Source de données (v1.2.0)

Depuis la v1.2.0, le pipeline peut utiliser deux sources, sélectionnées via `--source` (défaut `auto`) :

- **API officielle Météo-France** (`portail-api.meteofrance.fr`, abonnement payant "Modèle ARPÈGE API v1.0", service WCS `MF-NWP-GLOBAL-ARPEGE-005-EURAT-WCS`, résolution 0,05°) — **source par défaut** dès que la variable d'environnement `METEOFRANCE_API_KEY` est définie. Elle donne accès à des couches en altitude auparavant indisponibles (température/vent/humidité 850-500-300 hPa, géopotentiel, vitesse verticale à 500 hPa, CIN, vent à 100 m).
- **data.gouv.fr** (paquets ARPEGE 0,1° publics, sans clé) — source historique, utilisée automatiquement en repli (`fallback`) si l'API échoue ou si `METEOFRANCE_API_KEY` n'est pas définie, et utilisable explicitement avec `--source legacy`.

`--source api` force l'API et fait échouer le pipeline sans repli si elle est indisponible (utile pour du diagnostic). `--source auto` (par défaut dans le workflow GitHub Actions) essaie l'API puis retombe silencieusement sur data.gouv.fr en cas de problème (clé invalide, indicateur introuvable, erreur réseau) — **aucune donnée n'est perdue si l'intégration API rencontre un souci**, le pipeline continue de publier avec la source historique.

**Important — non testé en conditions réelles avec une vraie clé API** dans cet environnement de développement (aucune clé Météo-France de production ni de test n'y est accessible). Le client HTTP (`scripts/meteofrance_wcs_client.py`) reproduit fidèlement le schéma d'authentification et la syntaxe WCS 2.0.1 du client communautaire open-source [MAIF/meteole](https://github.com/MAIF/meteole) (vérifiés en lisant son code source), mais les noms exacts des indicateurs GRIB exposés par `GetCapabilities` pour ARPEGE-005-EURAT n'ont pas pu être confirmés. Voir la section « Tester la source API » ci-dessous pour valider vous-même avec votre clé.

### Tester la source API avec votre clé

```bash
python -m pip install -r requirements.txt
export METEOFRANCE_API_KEY="votre_clé_portail_api_meteofrance"
python scripts/update_arpege_france.py \
  --catalog config/communes-france.json \
  --output-dir build/national-api \
  --forecast-hours 12 \
  --source api
```

Avec `--source api`, toute erreur (clé invalide, indicateur introuvable, dépassement de quota) fait échouer le script explicitement au lieu de basculer en silence — c'est le mode recommandé pour un premier test. Les journaux (niveau `INFO`) indiquent, pour chaque champ, l'indicateur ARPEGE effectivement résolu (`Champ 'xxx' résolu vers l'indicateur ARPEGE 'YYY'`) : à vérifier une première fois contre `GetCapabilities` pour confirmer que la correspondance est correcte, en particulier pour les nouveaux champs d'altitude. En cas de désaccord, ajuster les motifs (`indicator_keywords`/`exclude_keywords`) dans `scripts/arpege_api_source.py` (table `FIELD_REQUESTS`).

## Ce que produit le workflow

- ARPEGE 0,1° (data.gouv.fr) ou 0,05° (API officielle, Euro-Atlantique), échéances horaires jusqu'à +102 h ;
- température, point de rosée, refroidissement éolien et humidex ;
- pluie horaire/cumulée, neige et cumul de neige fraîche estimé ;
- vent moyen, rafales, pression au sol et pression mer estimée ;
- nébulosité totale, basse, moyenne et élevée, humidité ;
- MUCAPE direct ARPEGE ;
- altitude du point de grille pour chaque commune ;
- **avec l'API officielle Météo-France uniquement** : température/vent/humidité à 850/500/300 hPa, géopotentiel à 850/500 hPa, vitesse verticale à 500 hPa, CIN et vent à 100 m ;
- 26+ couches cartographiques (jusqu'à 29 avec l'API) à plages nettes, frontières vectorielles, noms de communes, zoom jusqu'à 6 400 % et valeur sous la souris.

Les champs directs et les diagnostics dérivés sont distingués dans `index.json` et dans l'interface. Aucun champ vertical ou indice non présent dans les paquets ouverts SP1/SP2/HP1 n'est inventé.

## Installation du dépôt GitHub

1. Créez un dépôt GitHub, puis copiez tout le contenu de ce dossier à sa racine.
2. Dans **Settings → Actions → General → Workflow permissions**, choisissez **Read and write permissions**.
3. Lancez **Actions → Mise à jour ARPEGE France → Run workflow**.
4. À la fin du premier lancement, vérifiez la présence de la branche `data` et de son fichier `index.json`.

Le workflow est aussi lancé toutes les 3 heures (ARPEGE n'est republié que 4x/jour par Météo-France). Le script compare le run publié et ne reconstruit rien lorsqu'il n'existe pas de nouveau run complet.

**Aucune clé API n'est requise pour fonctionner** : sans `METEOFRANCE_API_KEY`, le workflow utilise directement les paquets ARPEGE 0,1° publics de data.gouv.fr, comme avant la v1.2.0. Pendant la publication d'un nouveau run,
data.gouv.fr peut momentanément présenter SP1, SP2 et HP1 avec des horaires
différents. Le pipeline vérifie chaque horodatage, patiente jusqu'à trois
minutes et, si la synchronisation n'est pas terminée, clôt proprement le passage
sans toucher à la branche `data` ; le lancement suivant reprend tout seul.

Pour activer la source API officielle et débloquer les couches en altitude (recommandé, résolution 2× plus fine) :

1. Créez une clé sur [portail-api.meteofrance.fr](https://portail-api.meteofrance.fr/) pour le produit **Modèle ARPÈGE API v1.0**.
2. Ajoutez-la comme secret GitHub Actions nommé `METEOFRANCE_API_KEY` (**Settings → Secrets and variables → Actions → New repository secret**).
3. Rien d'autre à faire : le workflow (`--source auto`) l'utilise automatiquement au prochain passage, avec repli sur data.gouv.fr en cas de problème.

Commande équivalente en local (source historique, sans clé) :

```bash
python -m pip install -r requirements.txt
python scripts/update_arpege_france.py \
  --catalog config/communes-france.json \
  --output-dir build/national \
  --forecast-hours 72
```

Le traitement télécharge successivement les paquets SP1 et SP2, ainsi que HP1 à +0 h pour l'altitude. Il ne conserve pas tous les GRIB simultanément afin de limiter l'espace disque du runner. Voir « Tester la source API avec votre clé » plus haut pour l'équivalent avec l'API officielle.

## Installation du module WordPress/Avada

Le ZIP du module se trouve dans la livraison séparée. Dans WordPress :

1. ouvrez **Extensions → Ajouter une extension → Téléverser une extension** ;
2. installez et activez le ZIP `arpege-meteofrance-france-v1.0.0.zip` ;
3. ouvrez **Réglages → ARPEGE Météo-France** et adaptez l'URL de la branche `data` si votre dépôt n'est pas `alertesmeteo-hub/arpege-meteo-france` ;
4. dans Avada Builder, ajoutez un élément **Code Block** ou **Text Block** contenant :

```text
[arpege_meteo]
```

Tout est intégré dans ce shortcode unique : recherche par commune ou code postal, géolocalisation, altitude, cartes, prévisions générales, tableaux orages/neige et quatre graphiques.

Exemple avec une ville initiale différente :

```text
[arpege_meteo ville="Paris" code="75056" departement="75" heures="48"]
```

## Structure publiée

```text
data/
├── index.json
├── departements/
│   ├── 01.json
│   ├── …
│   └── 95.json
└── maps/
    ├── index.json
    ├── communes.json
    ├── frontieres.svg
    ├── temperature/
    ├── pluie_1h/
    ├── reflectivite/
    └── values/
```

## Source et licence des données

- **Sans clé API** : [paquets ARPEGE 0,1° de Météo-France sur data.gouv.fr](https://www.data.gouv.fr/datasets/paquets-arpege-resolution-0-1deg), publiés sous Licence Ouverte 2.0.
- **Avec `METEOFRANCE_API_KEY`** : [API officielle Météo-France](https://portail-api.meteofrance.fr/web/fr/api/arpege), abonnement payant "Modèle ARPÈGE API v1.0", licence des données du portail API.

Les communes proviennent de l'API officielle de découpage administratif française.

Site : [www.alertes-meteo.com](https://www.alertes-meteo.com/) — module v1.2.0.

## Cartes disponibles

Cartes réparties par catégorie (Températures, Précipitations, Vent, Nuages et humidité, Pression et géopotentiel, Instabilité, Relief, Autres). Le menu de sélection distingue les paramètres essentiels (toujours visibles) des paramètres secondaires, repliés derrière un bouton « Voir plus de paramètres ».

Le socle de champs surface/2 m (SP1/SP2 côté data.gouv.fr) reste inchangé depuis la v1.1.0 : réflectivité radar et graupel restent absents (jamais publiés par ARPEGE), Tmin/Tmax 2 m, température de surface, hauteur de couche limite, eau précipitable, flux de chaleur sensible/latente et rayonnement solaire/thermique descendant restent disponibles quelle que soit la source.

**Nouveau en v1.2.0, uniquement avec l'API officielle Météo-France** (`METEOFRANCE_API_KEY` configurée) : températures et vent à 850/500/300 hPa, humidité à 850/500 hPa, géopotentiel à 850/500 hPa, vitesse verticale à 500 hPa, CIN et vent à 100 m. Ces couches restent vides (non publiées) tant qu'un run n'a pas été produit via l'API — voir « Tester la source API avec votre clé » pour valider la correspondance exacte des indicateurs Météo-France, jamais vérifiée en conditions réelles dans cet environnement de développement.

Restent non disponibles quelle que soit la source, car elles nécessitent un profil vertical complet ou ne sont pas produites par ARPEGE : ISO 0/-10/-20 °C, tourbillon/vorticité/divergence, densité de foudre, visibilité 2 m.

## Outils de la carte

La barre d'outils de la carte propose deux modes, à côté du sélecteur de paramètre et de la navigation dans les échéances :

- **Outil capture** : affiche des outils supplémentaires — capture PNG de la vue affichée et épinglage de la valeur au clic (en plus du survol).
- **Diagramme** : un clic sur la carte affiche un mini-diagramme (température et précipitations horaires) pour la commune la plus proche du point cliqué, à partir des mêmes données que l'onglet « Prévisions générales ».

Radiosondage et coupes verticales ne sont pas encore disponibles : ces vues nécessitent les niveaux de pression ARPEGE (paquet **IP1**), que le pipeline ne télécharge pas encore — seuls SP1/SP2/HP1 (champs de surface) sont utilisés aujourd'hui.
