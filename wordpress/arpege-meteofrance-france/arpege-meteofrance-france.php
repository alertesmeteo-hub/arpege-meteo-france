<?php
/**
 * Plugin Name: ARPEGE Météo-France France — Tableaux et cartes
 * Plugin URI: https://github.com/alertesmeteo-hub/arpege-meteo-france
 * Description: Module unique de cartes interactives et de prévisions ARPEGE de Météo-France pour la France métropolitaine et la Corse.
 * Version: 1.0.0
 * Author: Alertes Météo Hub
 * Requires at least: 5.8
 * Requires PHP: 7.4
 * License: GPL-2.0-or-later
 */

if (!defined('ABSPATH')) {
    exit;
}

define('ARP_VERSION', '1.0.0');
define('ARP_RELEASE_DATE', '04/09/2026');
define('ARP_OPTION_BASE_URL', 'arp_national_data_base_url');
define(
    'ARP_DEFAULT_BASE_URL',
    'https://raw.githubusercontent.com/alertesmeteo-hub/arpege-meteo-france/data'
);

// Auto-guérison du pipeline ARPEGE : si index.json est resté bloqué trop
// longtemps (cron GitHub Actions peu fiable), chaque chargement de la page
// relance côté serveur un nouveau run via workflow_dispatch. Le jeton
// GitHub reste EXCLUSIVEMENT côté serveur — à définir dans wp-config.php :
//   define('ARP_GITHUB_TOKEN', 'github_pat_xxx...');
// Jeton « fine-grained », limité au dépôt alertesmeteo-hub/arpege-meteo-france,
// permission « Actions » en Read and write.
define('ARP_GITHUB_REPO', 'alertesmeteo-hub/arpege-meteo-france');
define('ARP_GITHUB_DATA_BRANCH', 'data');
define('ARP_GITHUB_WORKFLOW_BRANCH', 'main');
define('ARP_GITHUB_WORKFLOW_FILE', 'update-arpege.yml');
// ARPEGE n'est republié que 4x/jour (00/06/12/18 UTC, cron toutes les 3h,
// minute 17) : seuil plus large que pour AROME, cohérent avec ce rythme.
define('ARP_STALE_THRESHOLD_MIN', 8 * 60);

add_action('wp_enqueue_scripts', 'arp_register_assets');
add_action('admin_init', 'arp_register_settings');
add_action('admin_menu', 'arp_add_settings_page');
add_shortcode('arpege_meteo', 'arp_render_shortcode');
add_filter('plugin_action_links_' . plugin_basename(__FILE__), 'arp_plugin_action_links');
add_action('wp_ajax_arp_autoheal', 'arp_handle_autoheal');
add_action('wp_ajax_nopriv_arp_autoheal', 'arp_handle_autoheal');

function arp_handle_autoheal() {
    if (!defined('ARP_GITHUB_TOKEN') || !ARP_GITHUB_TOKEN) {
        wp_send_json_success(array('configured' => false));
    }

    if (get_transient('arp_autoheal_lock')) {
        wp_send_json_success(array('skipped' => true));
    }
    set_transient('arp_autoheal_lock', 1, 5 * MINUTE_IN_SECONDS);

    $generated_at = arp_fetch_generated_at();
    if (null === $generated_at) {
        wp_send_json_success(array('configured' => true, 'checked' => false));
    }

    $age_minutes = (time() - $generated_at) / 60;
    if ($age_minutes <= ARP_STALE_THRESHOLD_MIN) {
        wp_send_json_success(array('configured' => true, 'stale' => false, 'age_minutes' => round($age_minutes)));
    }

    if (get_transient('arp_autoheal_cooldown')) {
        wp_send_json_success(array('configured' => true, 'stale' => true, 'triggered' => false, 'cooldown' => true));
    }
    set_transient('arp_autoheal_cooldown', 1, 30 * MINUTE_IN_SECONDS);

    $triggered = arp_trigger_workflow();
    wp_send_json_success(array('configured' => true, 'stale' => true, 'triggered' => $triggered));
}

function arp_fetch_generated_at() {
    $url = 'https://api.github.com/repos/' . ARP_GITHUB_REPO . '/contents/index.json'
        . '?ref=' . rawurlencode(ARP_GITHUB_DATA_BRANCH);
    $response = wp_remote_get($url, array(
        'headers' => array(
            'Accept'     => 'application/vnd.github.raw',
            'User-Agent' => 'arpege-meteofrance-france-autoheal',
        ),
        'timeout' => 8,
    ));
    if (is_wp_error($response) || 200 !== wp_remote_retrieve_response_code($response)) {
        return null;
    }
    $data = json_decode(wp_remote_retrieve_body($response), true);
    if (empty($data['generated_at'])) {
        return null;
    }
    $timestamp = strtotime($data['generated_at']);
    return $timestamp ? $timestamp : null;
}

function arp_trigger_workflow() {
    $url = 'https://api.github.com/repos/' . ARP_GITHUB_REPO . '/actions/workflows/'
        . rawurlencode(ARP_GITHUB_WORKFLOW_FILE) . '/dispatches';
    $response = wp_remote_post($url, array(
        'headers' => array(
            'Accept'        => 'application/vnd.github+json',
            'Authorization' => 'Bearer ' . ARP_GITHUB_TOKEN,
            'Content-Type'  => 'application/json',
            'User-Agent'    => 'arpege-meteofrance-france-autoheal',
        ),
        'body'    => wp_json_encode(array('ref' => ARP_GITHUB_WORKFLOW_BRANCH)),
        'timeout' => 8,
    ));
    if (is_wp_error($response)) {
        return false;
    }
    $code = wp_remote_retrieve_response_code($response);
    return $code >= 200 && $code < 300;
}

function arp_plugin_action_links($links) {
    $settings_link = sprintf(
        '<a href="%s">%s</a>',
        esc_url(admin_url('options-general.php?page=arpege-meteofrance')),
        esc_html__('Réglages', 'arpege-meteofrance-france')
    );
    array_unshift($links, $settings_link);

    $help_link = sprintf(
        '<a href="%s">%s</a>',
        esc_url(admin_url('options-general.php?page=arpege-meteofrance')),
        esc_html__('Shortcodes / Aide', 'arpege-meteofrance-france')
    );
    array_unshift($links, $help_link);

    return $links;
}

function arp_register_assets() {
    wp_register_style(
        'arp-table',
        plugin_dir_url(__FILE__) . 'assets/arpege-meteo.css',
        array(),
        ARP_VERSION
    );
    wp_register_script(
        'arp-table',
        plugin_dir_url(__FILE__) . 'assets/arpege-meteo.js',
        array(),
        ARP_VERSION,
        true
    );
    wp_register_style(
        'arp-map',
        plugin_dir_url(__FILE__) . 'assets/arpege-map.css',
        array('arp-table'),
        ARP_VERSION
    );
    wp_register_script(
        'arp-map',
        plugin_dir_url(__FILE__) . 'assets/arpege-map.js',
        array(),
        ARP_VERSION,
        true
    );
    wp_localize_script('arp-table', 'ARP_AUTOHEAL', array(
        'url' => admin_url('admin-ajax.php?action=arp_autoheal'),
    ));
}

function arp_register_settings() {
    register_setting(
        'arp_settings',
        ARP_OPTION_BASE_URL,
        array(
            'type' => 'string',
            'sanitize_callback' => 'esc_url_raw',
            'default' => ARP_DEFAULT_BASE_URL,
        )
    );

    add_settings_section(
        'arp_main_section',
        'Source des données nationales',
        '__return_false',
        'arpege-meteofrance'
    );

    add_settings_field(
        'arp_data_base_url_field',
        'Adresse du dossier de données',
        'arp_render_url_field',
        'arpege-meteofrance',
        'arp_main_section'
    );
}

function arp_render_url_field() {
    $value = get_option(ARP_OPTION_BASE_URL, ARP_DEFAULT_BASE_URL);
    printf(
        '<input type="url" class="regular-text code" name="%1$s" value="%2$s" autocomplete="off">',
        esc_attr(ARP_OPTION_BASE_URL),
        esc_attr($value)
    );
    echo '<p class="description">Conservez l’adresse proposée : elle pointe vers la branche nationale « data » du dépôt.</p>';
}

function arp_add_settings_page() {
    add_options_page(
        'Tableau ARPEGE Météo-France France',
        'ARPEGE Météo-France',
        'manage_options',
        'arpege-meteofrance',
        'arp_render_settings_page'
    );
}

function arp_render_settings_page() {
    if (!current_user_can('manage_options')) {
        return;
    }
    ?>
    <div class="wrap">
        <h1>ARPEGE Météo-France France</h1>
        <form action="options.php" method="post">
            <?php
            settings_fields('arp_settings');
            do_settings_sections('arpege-meteofrance');
            submit_button();
            ?>
        </form>
        <p><strong>Version du module : <?php echo esc_html(ARP_VERSION); ?> (<?php echo esc_html(ARP_RELEASE_DATE); ?>)</strong></p>
        <h2>Shortcode unique</h2>
        <p><code>[arpege_meteo]</code> : cartes interactives, prévisions générales, orages, neige et graphiques.</p>
        <p><code>[arpege_meteo code="75056" departement="75" ville="Paris" heures="48"]</code></p>
        <p><code>[arpege_meteo code="66136" departement="66" ville="Perpignan" selecteur="non"]</code> : une seule ville, sans recherche.</p>
        <p>Le visiteur peut ensuite rechercher n’importe quelle commune ou saisir un code postal.</p>
        <h2>Auto-guérison du pipeline</h2>
        <p>
            Statut : <strong><?php echo (defined('ARP_GITHUB_TOKEN') && ARP_GITHUB_TOKEN) ? '✅ Configurée' : '⚠️ Non configurée'; ?></strong>
        </p>
        <p>
            Si <code>index.json</code> reste bloqué plus de <?php echo esc_html((int) round(ARP_STALE_THRESHOLD_MIN / 60)); ?> heures,
            chaque chargement de cette page relance automatiquement le pipeline sur GitHub. Pour l'activer, ajouter dans
            <code>wp-config.php</code> :
        </p>
        <p><code>define('ARP_GITHUB_TOKEN', 'github_pat_xxx...');</code></p>
        <p>
            Jeton « fine-grained » GitHub, limité au dépôt <code>alertesmeteo-hub/arpege-meteo-france</code>, permission
            « Actions : Read and write » uniquement. Il n'est jamais transmis au navigateur.
        </p>
    </div>
    <?php
}

function arp_base_url() {
    $url = get_option(ARP_OPTION_BASE_URL, ARP_DEFAULT_BASE_URL);
    return untrailingslashit(apply_filters('arp_national_data_base_url', $url));
}

function arp_department_code($value) {
    $code = strtoupper(trim((string) $value));
    return preg_match('/^(?:\d{2}|2A|2B)$/', $code) ? $code : '66';
}

function arp_commune_code($value) {
    $code = strtoupper(trim((string) $value));
    return preg_match('/^[0-9A-Z]{5}$/', $code) ? $code : '66136';
}

function arp_unique_identifier() {
    if (function_exists('wp_unique_id')) {
        return wp_unique_id('arp-city-');
    }
    return 'arp-city-' . wp_rand(1000, 999999);
}

function arp_map_variable($value) {
    $variable = strtolower(trim(sanitize_key((string) $value)));
    $allowed = array(
        'temperature',
        'temperature_ressentie',
        'thermometre_mouille',
        'point_rosee',
        'humidex',
        'pluie_1h',
        'pluie_cumul',
        'neige',
        'neige_au_sol',
        'equivalent_eau_neige',
        'graupel',
        'neige_graupel',
        'grele',
        'type_precipitation_severe',
        'vent',
        'rafales',
        'pression',
        'pression_surface',
        'nebulosite',
        'nuages_bas',
        'nuages_moyens',
        'nuages_eleves',
        'humidite',
        'mucape',
        'reflectivite',
        'altitude',
    );
    return in_array($variable, $allowed, true) ? $variable : 'temperature';
}

function arp_render_map_shortcode($atts) {
    $atts = shortcode_atts(
        array(
            'variable' => 'temperature',
            'hauteur' => '700',
            'titre' => 'Cartes ARPEGE France',
            'animation' => 'oui',
        ),
        $atts,
        'arpege_meteo'
    );

    $variable = arp_map_variable($atts['variable']);
    $height = max(440, min(900, absint($atts['hauteur'])));
    $title = trim(sanitize_text_field($atts['titre']));
    if ($title === '') {
        $title = 'Cartes ARPEGE France';
    }
    $animation_value = strtolower(trim(sanitize_text_field($atts['animation'])));
    $animation = !in_array($animation_value, array('non', '0', 'false', 'off'), true);
    $map_id = function_exists('wp_unique_id')
        ? wp_unique_id('arp-map-')
        : 'arp-map-' . wp_rand(1000, 999999);

    wp_enqueue_style('arp-map');
    wp_enqueue_script('arp-map');

    ob_start();
    ?>
    <section
        id="<?php echo esc_attr($map_id); ?>"
        class="arp-card arpm-card"
        data-arpm-app
        data-base-url="<?php echo esc_url(arp_base_url()); ?>"
        data-variable="<?php echo esc_attr($variable); ?>"
        data-timezone="<?php echo esc_attr(wp_timezone_string()); ?>"
        data-animation="<?php echo $animation ? '1' : '0'; ?>"
        data-module-version="<?php echo esc_attr(ARP_VERSION); ?>"
        style="--arpm-height: <?php echo esc_attr($height); ?>px"
    >
        <header class="arp-header arpm-header">
            <div>
                <p class="arp-kicker">MODÈLE HAUTE RÉSOLUTION • ÉCHÉANCES HORAIRES</p>
                <h2><?php echo esc_html($title); ?></h2>
                <p class="arp-meta" data-arpm-run>Chargement du dernier run ARPEGE…</p>
            </div>
            <div class="arp-badge">ARPEGE<br><strong>1,3 km</strong></div>
        </header>

        <div class="arpm-toolbar">
            <div class="arpm-field arpm-layer-picker">
                <span>Paramètre</span>
                <button
                    type="button"
                    class="arpm-layer-trigger"
                    data-arpm-menu-toggle
                    aria-expanded="false"
                    aria-controls="<?php echo esc_attr($map_id . '-layers'); ?>"
                >
                    <span data-arpm-current-layer>Température à 2 m</span>
                    <span class="arpm-layer-chevron" aria-hidden="true">⌄</span>
                </button>
            </div>
            <div class="arpm-tools" aria-label="Outils de la carte">
                <button
                    type="button"
                    class="arpm-tool-toggle"
                    data-arpm-tool="zoom"
                    aria-pressed="false"
                    title="Afficher les outils de capture et d’épinglage"
                >🔍 Zoom interactif</button>
                <button
                    type="button"
                    class="arpm-tool-toggle"
                    data-arpm-tool="diagram"
                    aria-pressed="false"
                    title="Cliquer sur la carte pour afficher le diagramme d’un point"
                >📈 Diagramme</button>
            </div>
            <div class="arpm-time-controls" aria-label="Navigation dans les échéances">
                <button type="button" data-arpm-previous title="Échéance précédente" aria-label="Échéance précédente">◀</button>
                <button type="button" data-arpm-play title="Lancer l’animation" aria-label="Lancer l’animation">▶</button>
                <button type="button" data-arpm-next title="Échéance suivante" aria-label="Échéance suivante">▶</button>
            </div>
            <div class="arpm-validity">
                <span>Prévision valable</span>
                <strong data-arpm-validity>—</strong>
                <small data-arpm-lead>—</small>
            </div>
        </div>

        <p class="arpm-tool-hint" data-arpm-tool-hint hidden></p>

        <div
            id="<?php echo esc_attr($map_id . '-layers'); ?>"
            class="arpm-layer-menu"
            data-arpm-layer-menu
            hidden
        >
            <div class="arpm-layer-menu-head">
                <div>
                    <strong>Choisir une carte ARPEGE</strong>
                    <small>Uniquement les paramètres disponibles dans la production Météo-France</small>
                </div>
                <button type="button" data-arpm-menu-close aria-label="Réduire le menu">×</button>
            </div>
            <div class="arpm-layer-grid" data-arpm-layer-grid></div>
        </div>

        <div class="arpm-period-selector" data-arpm-period hidden>
            <div class="arpm-period-head">
                <div>
                    <strong data-arpm-period-title>Période personnalisée</strong>
                    <small>Déplacez les deux curseurs pour choisir précisément le début et la fin.</small>
                </div>
                <span data-arpm-period-summary>—</span>
            </div>
            <div class="arpm-dual-range" data-arpm-dual-range>
                <div class="arpm-dual-range-track" aria-hidden="true"></div>
                <input data-arpm-period-start type="range" min="0" max="1" value="0" step="1" aria-label="Début de la période">
                <input data-arpm-period-end type="range" min="0" max="1" value="1" step="1" aria-label="Fin de la période">
            </div>
            <div class="arpm-period-values">
                <span><small>Du</small><strong data-arpm-period-start-label>—</strong></span>
                <span><small>Au</small><strong data-arpm-period-end-label>—</strong></span>
            </div>
        </div>

        <p class="arp-stale" data-arpm-stale role="status" hidden>
            Attention : la dernière production disponible a plus de 8 heures.
        </p>

        <div class="arpm-viewport" data-arpm-viewport role="img" aria-label="Carte météo ARPEGE interactive">
            <div class="arpm-scene" data-arpm-scene>
                <canvas class="arpm-weather-canvas" data-arpm-weather aria-hidden="true"></canvas>
                <canvas class="arpm-vector-canvas" data-arpm-vectors aria-hidden="true"></canvas>
            </div>
            <canvas class="arpm-label-canvas" data-arpm-labels aria-hidden="true"></canvas>
            <div class="arpm-probe" data-arpm-probe hidden>
                <strong data-arpm-probe-value>—</strong>
                <span data-arpm-probe-label>Valeur ARPEGE</span>
            </div>
            <div class="arpm-map-titlebar">
                <strong data-arpm-map-title>Carte ARPEGE</strong>
                <span data-arpm-map-run>Run ARPEGE —</span>
            </div>
            <div class="arpm-map-date" data-arpm-map-date>Échéance —</div>
            <div class="arpm-map-buttons" aria-label="Commandes de zoom">
                <span class="arpm-zoom-level" data-arpm-zoom-level>100 %</span>
                <button type="button" data-arpm-zoom-in title="Agrandir" aria-label="Agrandir">+</button>
                <button type="button" data-arpm-zoom-out title="Réduire" aria-label="Réduire">−</button>
                <button type="button" data-arpm-reset title="Recentrer" aria-label="Recentrer">⌂</button>
                <button type="button" data-arpm-fullscreen title="Plein écran" aria-label="Plein écran">⛶</button>
            </div>
            <div class="arpm-advanced-tools" data-arpm-advanced-tools hidden aria-label="Outils avancés">
                <button type="button" data-arpm-capture title="Capturer l’image affichée" aria-label="Capturer l’image affichée">📷 Capture PNG</button>
                <button type="button" data-arpm-pin title="Épingler la valeur au clic" aria-label="Épingler la valeur au clic" aria-pressed="false">📌 Figer la valeur</button>
            </div>
            <div class="arpm-diagram-popup" data-arpm-diagram-popup hidden>
                <header>
                    <strong data-arpm-diagram-title>—</strong>
                    <button type="button" data-arpm-diagram-close aria-label="Fermer le diagramme">×</button>
                </header>
                <div class="arpm-diagram-body" data-arpm-diagram-body>
                    <p class="arpm-diagram-status" data-arpm-diagram-status>Chargement…</p>
                </div>
            </div>
            <div class="arpm-legend" data-arpm-legend aria-label="Légende de la carte"></div>
            <a class="arpm-map-brand" href="https://www.alertes-meteo.com/" target="_blank" rel="noopener noreferrer">
                www.alertes-meteo.com • Module v<?php echo esc_html(ARP_VERSION); ?> (<?php echo esc_html(ARP_RELEASE_DATE); ?>)
            </a>
            <div class="arpm-loading" data-arpm-loading role="status">Chargement de la carte…</div>
            <div class="arpm-error" data-arpm-error role="alert" hidden></div>
        </div>

        <div class="arpm-timeline" data-arpm-timeline>
            <input data-arpm-slider type="range" min="0" max="0" value="0" step="1" aria-label="Échéance de prévision">
            <div class="arpm-timeline-labels"><span>Run</span><span>Échéance maximale</span></div>
        </div>

        <footer class="arp-footer">
            <span data-arpm-generated>Mise à jour en cours de lecture…</span>
            <span>
                Données météo directes :
                <a href="https://www.data.gouv.fr/datasets/paquets-arpege-resolution-0-01deg" target="_blank" rel="noopener noreferrer">ARPEGE 0,01° — Météo-France</a>
                • <a href="https://www.alertes-meteo.com/" target="_blank" rel="noopener noreferrer">www.alertes-meteo.com</a>
                • Module cartes v<?php echo esc_html(ARP_VERSION); ?> (<?php echo esc_html(ARP_RELEASE_DATE); ?>)
            </span>
        </footer>

        <noscript>
            <p class="arp-message arp-error">JavaScript doit être activé pour afficher les cartes.</p>
        </noscript>
    </section>
    <?php
    return ob_get_clean();
}

function arp_render_shortcode($atts) {
    $atts = shortcode_atts(
        array(
            'ville' => 'Perpignan',
            'code' => '66136',
            'departement' => '66',
            'heures' => '48',
            'titre' => '',
            'selecteur' => 'oui',
        ),
        $atts,
        'arpege_meteo'
    );

    $hours = max(1, min(48, absint($atts['heures'])));
    $city_name = sanitize_text_field($atts['ville']);
    if ($city_name === '') {
        $city_name = 'Perpignan';
    }
    $city_code = arp_commune_code($atts['code']);
    $department = arp_department_code($atts['departement']);
    $title_prefix = trim(sanitize_text_field($atts['titre']));
    if ($title_prefix === '') {
        $title_prefix = 'Prévisions ARPEGE';
    }
    $selector_value = strtolower(trim(sanitize_text_field($atts['selecteur'])));
    $show_selector = !in_array($selector_value, array('non', '0', 'false', 'off'), true);

    $input_id = arp_unique_identifier();
    $results_id = $input_id . '-results';
    $status_id = $input_id . '-status';

    wp_enqueue_style('arp-table');
    wp_enqueue_script('arp-table');
    wp_enqueue_style('arp-map');
    wp_enqueue_script('arp-map');

    ob_start();
    ?>
    <section
        class="arp-card arp-national"
        data-arp-app
        data-base-url="<?php echo esc_url(arp_base_url()); ?>"
        data-default-code="<?php echo esc_attr($city_code); ?>"
        data-default-department="<?php echo esc_attr($department); ?>"
        data-default-name="<?php echo esc_attr($city_name); ?>"
        data-hours="<?php echo esc_attr($hours); ?>"
        data-timezone="<?php echo esc_attr(wp_timezone_string()); ?>"
        data-title-prefix="<?php echo esc_attr($title_prefix); ?>"
        data-selector="<?php echo $show_selector ? '1' : '0'; ?>"
    >
        <header class="arp-header">
            <div>
                <p class="arp-kicker">MODÈLE HAUTE RÉSOLUTION • FRANCE MÉTROPOLITAINE</p>
                <h2 data-arp-title><?php echo esc_html($title_prefix . ' — ' . $city_name); ?></h2>
                <p class="arp-city-altitude" data-arp-altitude>Altitude de <?php echo esc_html($city_name); ?> : chargement…</p>
                <p class="arp-meta" data-arp-meta>Chargement du dernier run ARPEGE…</p>
            </div>
            <div class="arp-badge">ARPEGE<br><strong>1,3 km</strong></div>
        </header>

        <div class="arp-toolbar" <?php if (!$show_selector) : ?>hidden<?php endif; ?>>
            <div class="arp-search">
                <label for="<?php echo esc_attr($input_id); ?>">Choisissez votre commune</label>
                <div class="arp-search-control">
                    <span class="arp-search-icon" aria-hidden="true">⌕</span>
                    <input
                        id="<?php echo esc_attr($input_id); ?>"
                        class="arp-city-input"
                        type="search"
                        value="<?php echo esc_attr($city_name); ?>"
                        placeholder="Nom de commune ou code postal"
                        autocomplete="off"
                        spellcheck="false"
                        role="combobox"
                        aria-autocomplete="list"
                        aria-expanded="false"
                        aria-controls="<?php echo esc_attr($results_id); ?>"
                        aria-describedby="<?php echo esc_attr($status_id); ?>"
                    >
                </div>
                <button type="button" class="arp-locate-button" data-arp-locate>📍 Détecter ma ville</button>
                <div
                    id="<?php echo esc_attr($results_id); ?>"
                    class="arp-search-results"
                    role="listbox"
                    hidden
                ></div>
                <p
                    id="<?php echo esc_attr($status_id); ?>"
                    class="arp-search-status"
                    role="status"
                    aria-live="polite"
                >Saisissez au moins deux lettres ou un code postal.</p>
            </div>
            <div class="arp-coverage">
                <strong>34 746 communes</strong>
                <span>Métropole et Corse</span>
            </div>
        </div>

        <p class="arp-stale" data-arp-stale role="status" hidden>
            Attention : la dernière mise à jour disponible a plus de 8 heures.
        </p>

        <div class="arp-tabs" role="tablist" aria-label="Type de prévision ARPEGE">
            <button
                type="button"
                class="arp-tab arp-tab-map is-active"
                role="tab"
                aria-selected="true"
                data-arp-tab="map"
            >🗺️ Cartes météo</button>
            <button
                type="button"
                class="arp-tab"
                role="tab"
                aria-selected="false"
                data-arp-tab="general"
            >🌤️ Prévisions générales</button>
            <button
                type="button"
                class="arp-tab arp-tab-storm"
                role="tab"
                aria-selected="false"
                data-arp-tab="storms"
            >⛈️ Prévisions orages</button>
            <button
                type="button"
                class="arp-tab arp-tab-snow"
                role="tab"
                aria-selected="false"
                data-arp-tab="snow"
            >❄️ Risque de neige</button>
        </div>

        <div class="arp-panel arp-map-panel" data-arp-panel="map">
            <?php
            echo arp_render_map_shortcode(
                array(
                    'variable' => 'temperature',
                    'hauteur' => '760',
                    'titre' => 'Cartes ARPEGE France — résolution 1,3 km',
                    'animation' => 'oui',
                )
            );
            ?>
        </div>

        <div class="arp-panel" data-arp-panel="general" hidden>
            <div class="arp-table-wrap arp-general-wrap" role="region" aria-label="Prévisions horaires générales" tabindex="0">
                <table class="arp-table">
                    <thead>
                        <tr>
                            <th scope="col">Date</th>
                            <th scope="col">Heure</th>
                            <th scope="col">Temps</th>
                            <th scope="col">T°</th>
                            <th scope="col">Hum.</th>
                            <th scope="col">Pluie</th>
                            <th scope="col">Nuages</th>
                            <th scope="col">Vent</th>
                            <th scope="col">Rafales</th>
                            <th scope="col">Pression</th>
                        </tr>
                    </thead>
                    <tbody data-arp-body-general>
                        <tr>
                            <td colspan="10" class="arp-loading">Chargement des prévisions…</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <section class="arp-charts" data-arp-charts aria-label="Diagrammes ARPEGE">
                <article class="arp-chart-card">
                    <h3 data-arp-chart-title-temperature>Diagramme températures (°C)</h3>
                    <div class="arp-chart" data-arp-chart-temperature></div>
                </article>
                <article class="arp-chart-card">
                    <h3 data-arp-chart-title-pressure>Diagramme pression ramenée au niveau de la mer (hPa)</h3>
                    <div class="arp-chart" data-arp-chart-pressure></div>
                </article>
                <article class="arp-chart-card">
                    <h3 data-arp-chart-title-rain>Diagramme précipitations (mm)</h3>
                    <p class="arp-chart-total" data-arp-rain-total>Précipitations cumulées : —</p>
                    <div class="arp-chart" data-arp-chart-rain></div>
                </article>
                <article class="arp-chart-card">
                    <h3 data-arp-chart-title-wind>Diagramme rafales et vent moyen</h3>
                    <div class="arp-chart" data-arp-chart-wind></div>
                </article>
            </section>
        </div>

        <div class="arp-panel" data-arp-panel="storms" hidden>
            <p class="arp-storm-summary" data-arp-storm-summary>
                Diagnostic convectif ARPEGE 0,01° : chargement…
            </p>
            <div class="arp-top-scroll" data-arp-top-scroll="storms" aria-label="Navigation horizontale du tableau orages" hidden><div></div></div>
            <div class="arp-table-wrap arp-storm-wrap" data-arp-scroll-wrap="storms" role="region" aria-label="Prévisions horaires d'orages" tabindex="0">
                <table class="arp-table arp-storm-table">
                    <thead>
                        <tr>
                            <th scope="col">Date</th>
                            <th scope="col">Heure</th>
                            <th scope="col">Risque orage</th>
                            <th scope="col">MUCAPE</th>
                            <th scope="col">LCL estimé</th>
                            <th scope="col">Foudre</th>
                            <th scope="col">Grêle</th>
                            <th scope="col">Pluie conv.</th>
                            <th scope="col">Graupel</th>
                            <th scope="col">Pluie 1 h</th>
                            <th scope="col">Rafales</th>
                            <th scope="col">Type</th>
                            <th scope="col">Détails</th>
                        </tr>
                    </thead>
                    <tbody data-arp-body-storms>
                        <tr>
                            <td colspan="13" class="arp-loading">Chargement du diagnostic orageux…</td>
                        </tr>
                    </tbody>
                </table>
            </div>
            <p class="arp-storm-note">
                <strong>Lecture expert :</strong> la MUCAPE et la réflectivité maximale sont des sorties directes ARPEGE. Le risque, la foudre, la grêle et le type d’orage sont des diagnostics dérivés clairement signalés ; aucune valeur indisponible n’est inventée.
            </p>
        </div>

        <div class="arp-panel" data-arp-panel="snow" hidden>
            <p class="arp-snow-summary" data-arp-snow-summary>
                Diagnostic neige ARPEGE 0,01° : chargement…
            </p>
            <div class="arp-top-scroll" data-arp-top-scroll="snow" aria-label="Navigation horizontale du tableau neige" hidden><div></div></div>
            <div class="arp-table-wrap arp-snow-wrap" data-arp-scroll-wrap="snow" role="region" aria-label="Risque horaire de neige" tabindex="0">
                <table class="arp-table arp-snow-table">
                    <thead>
                        <tr>
                            <th scope="col">Date</th>
                            <th scope="col">Heure</th>
                            <th scope="col">Risque neige</th>
                            <th scope="col">Phase</th>
                            <th scope="col">Neige 1 h</th>
                            <th scope="col">Neige 3 h</th>
                            <th scope="col">Neige 6 h</th>
                            <th scope="col">Tenue</th>
                            <th scope="col">Pres. hPa</th>
                            <th scope="col">Hum.</th>
                            <th scope="col">Vent moy. / raf.</th>
                            <th scope="col">Cumul neige fraîche</th>
                            <th scope="col">Détails</th>
                        </tr>
                    </thead>
                    <tbody data-arp-body-snow>
                        <tr>
                            <td colspan="13" class="arp-loading">Chargement du risque de neige…</td>
                        </tr>
                    </tbody>
                </table>
            </div>
            <p class="arp-snow-note">
                <strong>Lecture neige :</strong> les cumuls de neige sont des sorties directes ARPEGE. La neige fraîche et la tenue sont estimées à partir du cumul en eau, de la température à 2 m et de l’altitude du point de grille.
            </p>
        </div>

        <footer class="arp-footer">
            <span data-arp-generated>Mise à jour en cours de lecture…</span>
            <span>
                Données météo directes :
                <a href="https://www.data.gouv.fr/datasets/paquets-arpege-resolution-0-01deg" target="_blank" rel="noopener noreferrer">ARPEGE 0,01° — Météo-France</a>
                • Recherche des communes :
                <a href="https://geo.api.gouv.fr/decoupage-administratif/communes" target="_blank" rel="noopener noreferrer">API officielle française</a>
                • <a href="https://www.alertes-meteo.com/" target="_blank" rel="noopener noreferrer">www.alertes-meteo.com</a>
            </span>
            <span class="arp-plugin-version">Module ARPEGE v<?php echo esc_html(ARP_VERSION); ?> (<?php echo esc_html(ARP_RELEASE_DATE); ?>)</span>
        </footer>

        <noscript>
            <p class="arp-message arp-error">JavaScript doit être activé pour rechercher une commune.</p>
        </noscript>
    </section>
    <?php
    return ob_get_clean();
}
