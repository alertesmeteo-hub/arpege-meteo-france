=== ARPEGE Météo-France France ===
Contributors: alertesmeteo
Tags: meteo, arpege, meteofrance, carte, previsions, avada
Requires at least: 5.8
Requires PHP: 7.4
Stable tag: 1.0.0
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

= 1.0.0 =
* Première version du module ARPEGE, dérivée du module AROME (même socle
  fonctionnel : carte, recherche, altitude, tableaux, graphiques, tableaux
  orages/neige, barre d'outils Zoom interactif/Diagramme).
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
