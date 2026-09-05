=== ARPEGE Météo-France France ===
Contributors: alertesmeteo
Tags: meteo, arpege, meteofrance, carte, previsions, avada
Requires at least: 5.8
Requires PHP: 7.4
Stable tag: 1.2.0
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Module unique de cartes interactives et prévisions ARPEGE pour 34 746 communes françaises.

== Description ==

Le shortcode [arpege_meteo] affiche dans un seul module :

* une carte ARPEGE interactive (grille EURAT01, 0,1°) avec zoom jusqu'à 6 400 %, noms de communes et valeur au survol ;
* une recherche par ville ou code postal et la géolocalisation ;
* l'altitude du point de grille ARPEGE ;
* les prévisions générales jusqu'à +102 h et quatre graphiques ;
* les diagnostics orages et neige.

Les données sont lues depuis la branche data du dépôt GitHub configuré dans Réglages > ARPEGE Météo-France.

ARPEGE est le modèle global de Météo-France : résolution plus grossière que le
modèle à aire limitée AROME (~10 km contre ~1,3 km) mais échéances nettement
plus longues (jusqu'à +102 h contre +51 h pour AROME).

Ce module est construit sur les mêmes fondations que le module AROME
(alertes-meteo.com), avec un namespace entièrement dédié (préfixes PHP `arp_`,
handles `arp-table`/`arp-map`, attributs `data-arpm-*`) pour permettre
l'activation simultanée des deux modules sans collision.

== Installation ==

1. Téléversez le ZIP dans Extensions > Ajouter une extension.
2. Activez ARPEGE Météo-France France.
3. Vérifiez l'URL des données dans Réglages > ARPEGE Météo-France.
4. Insérez [arpege_meteo] dans un bloc Avada.

== Changelog ==

= 1.2.0 =
* Le pipeline de production (dépôt GitHub, pas ce plugin) peut désormais
  utiliser l'API officielle Météo-France (portail-api.meteofrance.fr,
  abonnement "Modèle ARPÈGE API v1.0") comme source de données à la place du
  scraping data.gouv.fr, dès qu'une clé API est configurée côté GitHub
  Actions. Repli automatique sur data.gouv.fr en cas de problème.
* Débloque, uniquement quand la source API est active, les couches vent/
  température/humidité à 850/500/300 hPa, géopotentiel à 850/500 hPa,
  vitesse verticale à 500 hPa, CIN et vent à 100 m — jusqu'ici marquées
  indisponibles faute d'accès aux paquets isobares/hauteur.
* Aucun changement côté ce plugin WordPress lui-même (affichage, shortcode) :
  les nouvelles couches apparaissent automatiquement dans le sélecteur de
  carte dès qu'elles sont publiées par le pipeline.

= 1.1.0 =
* Bouton de la barre d'outils renommé « 📷 Outil capture » (était « 🔍 Zoom
  interactif », un intitulé trompeur : il ouvre l'outil de capture PNG et
  d'épinglage, il ne zoome pas).
* Frontières départementales redessinées : tracé plus fin et plus discret
  (trait à 0,45 px, gris clair semi-transparent au lieu du gris foncé à
  0,8 px) et construit depuis les ~35 000 communes brutes plutôt que depuis
  les points de grille ARPEGE 0,1° (~10 km) — le rendu "vitrail" anguleux
  signalé venait de la faible densité de points utilisée pour classer
  chaque pixel par département.
* Échéances : confirmé par le descriptif technique officiel Météo-France
  (paquets ARPEGE, EURAT01 0,1°, v. 02/01/2024) que +102 h est bien
  l'échéance maximale réellement publiée (9 tranches 00-12…97-102, aucun
  réseau ne va jusqu'à +114 h) ; le workflow de production passe de +72 h à
  +102 h pour exploiter toute l'échéance disponible.
* Retrait de la réflectivité radar et du graupel : absents des paquets
  SP1/SP2 ARPEGE réels (confirmés par le descriptif technique Météo-France),
  ces couches et cette colonne de tableau restaient toujours vides (NaN
  silencieux). Les indices dérivés (risque orage, foudre, grêle, type
  d'orage) reposent désormais uniquement sur le CAPE instantané, avec des
  seuils réévalués.
* Ajout de couches réellement présentes dans SP1/SP2 mais jusqu'ici non
  exploitées : Tmin/Tmax 2 m, température de surface, hauteur de couche
  limite, eau précipitable (colonne de vapeur d'eau), flux de chaleur
  sensible/latente cumulés, rayonnement solaire et thermique descendants.
* Les couches demandées nécessitant les paquets isobares/hauteur (vent,
  température et humidité à 850/500/300 hPa, géopotentiel, ISO 0/-10/-20°C,
  vitesse verticale, tourbillon/vorticité/divergence, CIN) ne sont pas
  intégrées dans cette version : elles exigent de télécharger et décoder
  IP1-4/HP1-2 (plusieurs centaines de Mo par tranche horaire), une extension
  de pipeline distincte non couverte ici. La densité de foudre n'est quant
  à elle publiée par aucun paquet ARPEGE (pas une sortie du modèle PNT).

= 1.0.0 =
* Première version du module ARPEGE, dérivée du module AROME (même socle
  fonctionnel : carte, recherche, altitude, tableaux, graphiques, tableaux
  orages/neige, barre d'outils Outil capture/Diagramme).
* Adapté au modèle global ARPEGE : grille EURAT01 0,1° (~10 km), échéances
  jusqu'à +102 h, dataset data.gouv.fr « Paquets ARPEGE résolution 0,1° ».
* Namespace entièrement renommé (PHP, JS, CSS, data-attributes, shortcode,
  handles d'enregistrement) pour éviter toute collision avec AROME ou tout
  autre module Alertes Météo actif simultanément sur le même site.
* Lien d'action « Shortcodes / Aide » et « Réglages » sur la page Extensions.
* Footer front-end affichant version et date de build.
* Auto-guérison du pipeline : si le run publié reste bloqué plus de 8 h,
  chaque chargement de la page relance automatiquement le pipeline sur
  GitHub via un jeton dédié côté serveur (`ARP_GITHUB_TOKEN` dans
  wp-config.php).
* Cartes toujours en attente du paquet ARPEGE IP1 (niveaux de pression) :
  Radiosondage et Coupes verticales non disponibles pour l'instant, seuls
  les paquets de surface SP1/SP2/HP1 sont utilisés aujourd'hui.
