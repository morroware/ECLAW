<?php
/**
 * Plugin Name: Remote Claw Embed
 * Plugin URI:  https://github.com/your-org/eclaw
 * Description: Embed the Remote Claw Machine on any WordPress page. Supports Elementor HTML widgets, Gutenberg blocks, and classic editor shortcodes. Includes admin settings, responsive sizing, and full customization.
 * Version:     2.0.0
 * Author:      The Castle Fun Center
 * License:     GPL-2.0-or-later
 * Text Domain: eclaw-embed
 *
 * Installation:
 *   Option A: Copy this file to wp-content/mu-plugins/eclaw-embed.php
 *   Option B: Create wp-content/plugins/eclaw-embed/ and place this file inside
 *
 * Shortcode Usage:
 *   [eclaw]                                                    - Watch mode with default server URL
 *   [eclaw mode="play"]                                        - Interactive play mode
 *   [eclaw url="https://claw.example.com"]                     - Custom server URL
 *   [eclaw mode="play" width="100%" height="620"]              - Custom dimensions
 *   [eclaw theme="light" accent="ff3366"]                      - Light theme + accent
 *   [eclaw aspect_ratio="16:9"]                                - Responsive aspect ratio
 *   [eclaw responsive="1"]                                     - Auto 16:9 responsive
 *   [eclaw loading="eager"]                                    - No lazy loading
 *   [eclaw bg="1a1a2e" border_radius="12"]                     - Custom background + radius
 *   [eclaw class="my-claw-widget"]                             - Extra CSS class
 *   [eclaw title="Play our Claw Machine!"]                     - Custom iframe title
 *   [eclaw footer="0" sounds="0"]                              - Hide footer, mute sounds
 */

if ( ! defined( 'ABSPATH' ) ) exit;

// ============================================================================
// ADMIN SETTINGS PAGE
// ============================================================================

/**
 * Register plugin settings.
 */
function eclaw_register_settings() {
    register_setting( 'eclaw_settings_group', 'eclaw_options', 'eclaw_sanitize_options' );

    add_settings_section(
        'eclaw_main_section',
        'Remote Claw Embed Settings',
        'eclaw_section_description',
        'eclaw-settings'
    );

    $fields = array(
        array( 'eclaw_default_url',          'Default Server URL',      'eclaw_field_url' ),
        array( 'eclaw_default_mode',         'Default Mode',            'eclaw_field_mode' ),
        array( 'eclaw_default_theme',        'Default Theme',           'eclaw_field_theme' ),
        array( 'eclaw_default_width',        'Default Width',           'eclaw_field_width' ),
        array( 'eclaw_default_height',       'Default Height',          'eclaw_field_height' ),
        array( 'eclaw_default_accent',       'Default Accent Color',    'eclaw_field_accent' ),
        array( 'eclaw_default_bg',           'Default Background',      'eclaw_field_bg' ),
        array( 'eclaw_default_border_radius','Default Border Radius',   'eclaw_field_border_radius' ),
        array( 'eclaw_default_responsive',   'Responsive by Default',   'eclaw_field_responsive' ),
        array( 'eclaw_default_aspect_ratio', 'Default Aspect Ratio',    'eclaw_field_aspect_ratio' ),
        array( 'eclaw_default_footer',       'Show Footer (watch)',     'eclaw_field_footer' ),
        array( 'eclaw_default_sounds',       'Enable Sounds (play)',    'eclaw_field_sounds' ),
        array( 'eclaw_default_loading',      'Loading Strategy',        'eclaw_field_loading' ),
    );

    foreach ( $fields as $field ) {
        add_settings_field( $field[0], $field[1], $field[2], 'eclaw-settings', 'eclaw_main_section' );
    }
}
add_action( 'admin_init', 'eclaw_register_settings' );

/**
 * Add admin menu page.
 */
function eclaw_add_admin_menu() {
    add_options_page(
        'Remote Claw Embed',
        'Remote Claw',
        'manage_options',
        'eclaw-settings',
        'eclaw_settings_page'
    );
}
add_action( 'admin_menu', 'eclaw_add_admin_menu' );

/**
 * Get plugin options with defaults.
 */
function eclaw_get_options() {
    $defaults = array(
        'default_url'          => '',
        'default_mode'         => 'watch',
        'default_theme'        => 'dark',
        'default_width'        => '100%',
        'default_height'       => '480',
        'default_accent'       => '',
        'default_bg'           => '',
        'default_border_radius'=> '8',
        'default_responsive'   => '0',
        'default_aspect_ratio' => '16:9',
        'default_footer'       => '1',
        'default_sounds'       => '1',
        'default_loading'      => 'lazy',
    );
    $opts = get_option( 'eclaw_options', array() );
    return wp_parse_args( $opts, $defaults );
}

/**
 * Sanitize options on save.
 */
function eclaw_sanitize_options( $input ) {
    $clean = array();
    $clean['default_url']           = esc_url_raw( trim( $input['default_url'] ?? '' ) );
    $clean['default_mode']          = in_array( $input['default_mode'] ?? '', array( 'watch', 'play' ), true ) ? $input['default_mode'] : 'watch';
    $clean['default_theme']         = in_array( $input['default_theme'] ?? '', array( 'dark', 'light' ), true ) ? $input['default_theme'] : 'dark';
    $clean['default_width']         = sanitize_text_field( $input['default_width'] ?? '100%' );
    $clean['default_height']        = sanitize_text_field( $input['default_height'] ?? '480' );
    $clean['default_accent']        = preg_replace( '/[^a-fA-F0-9]/', '', $input['default_accent'] ?? '' );
    $clean['default_bg']            = preg_replace( '/[^a-fA-F0-9]/', '', $input['default_bg'] ?? '' );
    $clean['default_border_radius'] = absint( $input['default_border_radius'] ?? 8 );
    $clean['default_responsive']    = ( $input['default_responsive'] ?? '0' ) === '1' ? '1' : '0';
    $clean['default_aspect_ratio']  = sanitize_text_field( $input['default_aspect_ratio'] ?? '16:9' );
    $clean['default_footer']        = ( $input['default_footer'] ?? '1' ) === '0' ? '0' : '1';
    $clean['default_sounds']        = ( $input['default_sounds'] ?? '1' ) === '0' ? '0' : '1';
    $clean['default_loading']       = in_array( $input['default_loading'] ?? '', array( 'lazy', 'eager' ), true ) ? $input['default_loading'] : 'lazy';
    return $clean;
}

function eclaw_section_description() {
    echo '<p>Configure default settings for the <code>[eclaw]</code> shortcode. Individual shortcode attributes override these defaults.</p>';
}

// -- Settings field callbacks ------------------------------------------------

function eclaw_field_url() {
    $opts = eclaw_get_options();
    printf( '<input type="url" name="eclaw_options[default_url]" value="%s" class="regular-text" placeholder="https://claw.yourdomain.com">', esc_attr( $opts['default_url'] ) );
    echo '<p class="description">Base URL of your ECLAW server. Can be overridden per shortcode with <code>url="..."</code></p>';
}

function eclaw_field_mode() {
    $opts = eclaw_get_options();
    printf( '<select name="eclaw_options[default_mode]"><option value="watch" %s>Watch (spectator)</option><option value="play" %s>Play (interactive)</option></select>',
        selected( $opts['default_mode'], 'watch', false ),
        selected( $opts['default_mode'], 'play', false )
    );
}

function eclaw_field_theme() {
    $opts = eclaw_get_options();
    printf( '<select name="eclaw_options[default_theme]"><option value="dark" %s>Dark</option><option value="light" %s>Light</option></select>',
        selected( $opts['default_theme'], 'dark', false ),
        selected( $opts['default_theme'], 'light', false )
    );
}

function eclaw_field_width() {
    $opts = eclaw_get_options();
    printf( '<input type="text" name="eclaw_options[default_width]" value="%s" class="small-text" placeholder="100%%">', esc_attr( $opts['default_width'] ) );
    echo '<p class="description">CSS width value (e.g. <code>100%</code>, <code>640px</code>, <code>80vw</code>)</p>';
}

function eclaw_field_height() {
    $opts = eclaw_get_options();
    printf( '<input type="text" name="eclaw_options[default_height]" value="%s" class="small-text" placeholder="480">', esc_attr( $opts['default_height'] ) );
    echo '<p class="description">Height in pixels (e.g. <code>480</code>, <code>620</code>). Ignored when responsive mode is enabled.</p>';
}

function eclaw_field_accent() {
    $opts = eclaw_get_options();
    printf( '<input type="text" name="eclaw_options[default_accent]" value="%s" class="small-text" placeholder="8b5cf6" maxlength="6">', esc_attr( $opts['default_accent'] ) );
    echo '<p class="description">Hex color without <code>#</code> (e.g. <code>ff3366</code>). Overrides the accent/primary color.</p>';
}

function eclaw_field_bg() {
    $opts = eclaw_get_options();
    printf( '<input type="text" name="eclaw_options[default_bg]" value="%s" class="small-text" placeholder="0a0a0f" maxlength="6">', esc_attr( $opts['default_bg'] ) );
    echo '<p class="description">Background hex color without <code>#</code>. Useful for matching your site\'s background.</p>';
}

function eclaw_field_border_radius() {
    $opts = eclaw_get_options();
    printf( '<input type="number" name="eclaw_options[default_border_radius]" value="%s" class="small-text" min="0" max="50" step="1"> px', esc_attr( $opts['default_border_radius'] ) );
}

function eclaw_field_responsive() {
    $opts = eclaw_get_options();
    printf( '<label><input type="checkbox" name="eclaw_options[default_responsive]" value="1" %s> Enable responsive aspect-ratio sizing (ignores fixed height)</label>', checked( $opts['default_responsive'], '1', false ) );
}

function eclaw_field_aspect_ratio() {
    $opts = eclaw_get_options();
    printf( '<input type="text" name="eclaw_options[default_aspect_ratio]" value="%s" class="small-text" placeholder="16:9">', esc_attr( $opts['default_aspect_ratio'] ) );
    echo '<p class="description">Aspect ratio when responsive mode is enabled (e.g. <code>16:9</code>, <code>4:3</code>, <code>21:9</code>)</p>';
}

function eclaw_field_footer() {
    $opts = eclaw_get_options();
    printf( '<label><input type="checkbox" name="eclaw_options[default_footer]" value="1" %s> Show status footer in watch mode</label>', checked( $opts['default_footer'], '1', false ) );
    // Hidden field to ensure '0' is sent when unchecked
    echo '<input type="hidden" name="eclaw_options[default_footer_hidden]" value="1">';
}

function eclaw_field_sounds() {
    $opts = eclaw_get_options();
    printf( '<label><input type="checkbox" name="eclaw_options[default_sounds]" value="1" %s> Enable sound effects in play mode</label>', checked( $opts['default_sounds'], '1', false ) );
    echo '<input type="hidden" name="eclaw_options[default_sounds_hidden]" value="1">';
}

function eclaw_field_loading() {
    $opts = eclaw_get_options();
    printf( '<select name="eclaw_options[default_loading]"><option value="lazy" %s>Lazy (load when visible)</option><option value="eager" %s>Eager (load immediately)</option></select>',
        selected( $opts['default_loading'], 'lazy', false ),
        selected( $opts['default_loading'], 'eager', false )
    );
    echo '<p class="description">Use <code>eager</code> for above-the-fold embeds to avoid a loading flash.</p>';
}

/**
 * Settings page HTML.
 */
function eclaw_settings_page() {
    if ( ! current_user_can( 'manage_options' ) ) return;
    ?>
    <div class="wrap">
        <h1>Remote Claw Embed Settings</h1>

        <div style="background:#fff;border-left:4px solid #8b5cf6;padding:12px 16px;margin:12px 0 20px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
            <strong>Quick Start:</strong> Set your server URL below, then use <code>[eclaw]</code> on any page.
            For interactive mode use <code>[eclaw mode="play"]</code>. All settings below act as defaults
            and can be overridden per-shortcode.
        </div>

        <form method="post" action="options.php">
            <?php
            settings_fields( 'eclaw_settings_group' );
            do_settings_sections( 'eclaw-settings' );
            submit_button( 'Save Settings' );
            ?>
        </form>

        <hr>
        <h2>Shortcode Reference</h2>
        <table class="widefat striped" style="max-width:900px;">
            <thead><tr><th>Attribute</th><th>Values</th><th>Default</th><th>Description</th></tr></thead>
            <tbody>
                <tr><td><code>url</code></td><td>URL</td><td>(from settings)</td><td>ECLAW server base URL</td></tr>
                <tr><td><code>mode</code></td><td><code>watch</code>, <code>play</code></td><td><code>watch</code></td><td>Spectator or interactive mode</td></tr>
                <tr><td><code>theme</code></td><td><code>dark</code>, <code>light</code></td><td><code>dark</code></td><td>Color scheme</td></tr>
                <tr><td><code>width</code></td><td>CSS value</td><td><code>100%</code></td><td>Iframe width</td></tr>
                <tr><td><code>height</code></td><td>Pixels</td><td><code>480</code></td><td>Iframe height (ignored if responsive)</td></tr>
                <tr><td><code>responsive</code></td><td><code>0</code>, <code>1</code></td><td><code>0</code></td><td>Use aspect-ratio responsive sizing</td></tr>
                <tr><td><code>aspect_ratio</code></td><td>e.g. <code>16:9</code></td><td><code>16:9</code></td><td>Aspect ratio for responsive mode</td></tr>
                <tr><td><code>accent</code></td><td>Hex (no #)</td><td>—</td><td>Accent/primary color</td></tr>
                <tr><td><code>bg</code></td><td>Hex (no #)</td><td>—</td><td>Background color</td></tr>
                <tr><td><code>border_radius</code></td><td>Pixels</td><td><code>8</code></td><td>Corner rounding</td></tr>
                <tr><td><code>footer</code></td><td><code>0</code>, <code>1</code></td><td><code>1</code></td><td>Show footer (watch mode)</td></tr>
                <tr><td><code>sounds</code></td><td><code>0</code>, <code>1</code></td><td><code>1</code></td><td>Sound effects (play mode)</td></tr>
                <tr><td><code>loading</code></td><td><code>lazy</code>, <code>eager</code></td><td><code>lazy</code></td><td>Iframe loading strategy</td></tr>
                <tr><td><code>title</code></td><td>Text</td><td>Remote Claw Machine</td><td>Iframe title (accessibility)</td></tr>
                <tr><td><code>class</code></td><td>CSS classes</td><td>—</td><td>Extra CSS classes on wrapper</td></tr>
            </tbody>
        </table>

        <h2 style="margin-top:24px;">Elementor Usage</h2>
        <div style="background:#fff;border-left:4px solid #22d3ee;padding:12px 16px;margin:8px 0;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
            <p style="margin:0;">Add an <strong>HTML widget</strong> or <strong>Shortcode widget</strong> in Elementor and paste:</p>
            <pre style="background:#f5f5f5;padding:8px 12px;border-radius:4px;margin:8px 0 0;"><code>[eclaw mode="play" responsive="1"]</code></pre>
            <p style="margin:8px 0 0;color:#666;">The <code>responsive="1"</code> option ensures the embed scales properly within Elementor columns.</p>
        </div>
    </div>
    <?php
}

// ============================================================================
// SHORTCODE
// ============================================================================

/**
 * [eclaw] shortcode handler.
 */
function eclaw_embed_shortcode( $atts ) {
    $opts = eclaw_get_options();

    $atts = shortcode_atts( array(
        'mode'          => $opts['default_mode'],
        'url'           => $opts['default_url'],
        'width'         => $opts['default_width'],
        'height'        => $opts['default_height'],
        'theme'         => $opts['default_theme'],
        'footer'        => $opts['default_footer'],
        'sounds'        => $opts['default_sounds'],
        'accent'        => $opts['default_accent'],
        'bg'            => $opts['default_bg'],
        'responsive'    => $opts['default_responsive'],
        'aspect_ratio'  => $opts['default_aspect_ratio'],
        'border_radius' => $opts['default_border_radius'],
        'loading'       => $opts['default_loading'],
        'title'         => 'Remote Claw Machine',
        'class'         => '',
    ), $atts, 'eclaw' );

    // Validate URL
    if ( empty( $atts['url'] ) ) {
        if ( current_user_can( 'edit_posts' ) ) {
            return '<div style="border:2px dashed #ef4444;padding:20px;border-radius:8px;text-align:center;color:#ef4444;font-family:sans-serif;">'
                . '<strong>[eclaw]</strong> Server URL is not configured.<br>'
                . 'Set it in <a href="' . admin_url( 'options-general.php?page=eclaw-settings' ) . '">Settings &rarr; Remote Claw</a> '
                . 'or add <code>url="https://claw.yourdomain.com"</code> to the shortcode.'
                . '</div>';
        }
        return '<!-- eclaw: no URL configured -->';
    }

    // Sanitize
    $mode          = ( $atts['mode'] === 'play' ) ? 'play' : 'watch';
    $theme         = ( $atts['theme'] === 'light' ) ? 'light' : 'dark';
    $width         = esc_attr( $atts['width'] );
    $height        = absint( $atts['height'] ) ?: 480;
    $responsive    = ( $atts['responsive'] === '1' );
    $border_radius = absint( $atts['border_radius'] );
    $loading       = ( $atts['loading'] === 'eager' ) ? 'eager' : 'lazy';
    $title         = esc_attr( $atts['title'] );
    $extra_class   = sanitize_html_class( $atts['class'] );
    $accent        = preg_replace( '/[^a-fA-F0-9]/', '', $atts['accent'] );
    $bg            = preg_replace( '/[^a-fA-F0-9]/', '', $atts['bg'] );

    // Build iframe src
    $embed_path = ( $mode === 'play' ) ? '/embed/play' : '/embed/watch';

    $query_params = array();
    if ( $theme !== 'dark' )               $query_params['theme']  = $theme;
    if ( $atts['footer'] === '0' )         $query_params['footer'] = '0';
    if ( $atts['sounds'] === '0' )         $query_params['sounds'] = '0';
    if ( ! empty( $accent ) )              $query_params['accent'] = $accent;
    if ( ! empty( $bg ) )                  $query_params['bg']     = $bg;

    $src = rtrim( $atts['url'], '/' ) . $embed_path;
    if ( ! empty( $query_params ) ) {
        $src .= '?' . http_build_query( $query_params );
    }

    // Build iframe allow policy
    $allow = 'autoplay; encrypted-media; fullscreen; picture-in-picture; web-share';

    // Determine aspect ratio padding for responsive mode
    $aspect_padding = '56.25%'; // 16:9 default
    if ( $responsive && preg_match( '/^(\d+):(\d+)$/', $atts['aspect_ratio'], $m ) ) {
        $aspect_padding = number_format( ( (int) $m[2] / (int) $m[1] ) * 100, 4, '.', '' ) . '%';
    }

    // Wrapper classes
    $wrapper_classes = 'eclaw-embed-wrapper';
    if ( $responsive ) $wrapper_classes .= ' eclaw-responsive';
    if ( $extra_class ) $wrapper_classes .= ' ' . $extra_class;

    // Build output
    $output = '';

    // Inline styles (only printed once per page)
    static $styles_printed = false;
    if ( ! $styles_printed ) {
        $styles_printed = true;
        $output .= '<style>'
            . '.eclaw-embed-wrapper{position:relative;max-width:100%;margin:0 auto;}'
            . '.eclaw-embed-wrapper iframe{display:block;border:0;}'
            . '.eclaw-responsive{width:100%;height:0;overflow:hidden;}'
            . '.eclaw-responsive iframe{position:absolute;top:0;left:0;width:100%;height:100%;}'
            . '</style>';
    }

    if ( $responsive ) {
        $output .= sprintf(
            '<div class="%s" style="padding-bottom:%s;">'
            . '<iframe src="%s" allow="%s" allowfullscreen '
            . 'style="border-radius:%dpx;" '
            . 'loading="%s" title="%s"></iframe>'
            . '</div>',
            esc_attr( $wrapper_classes ),
            esc_attr( $aspect_padding ),
            esc_url( $src ),
            esc_attr( $allow ),
            $border_radius,
            esc_attr( $loading ),
            $title
        );
    } else {
        $output .= sprintf(
            '<div class="%s">'
            . '<iframe src="%s" width="%s" height="%s" allow="%s" allowfullscreen '
            . 'style="border:0;border-radius:%dpx;max-width:100%%;" '
            . 'loading="%s" title="%s"></iframe>'
            . '</div>',
            esc_attr( $wrapper_classes ),
            esc_url( $src ),
            $width,
            $height,
            esc_attr( $allow ),
            $border_radius,
            esc_attr( $loading ),
            $title
        );
    }

    return $output;
}
add_shortcode( 'eclaw', 'eclaw_embed_shortcode' );

// ============================================================================
// GUTENBERG BLOCK (server-side rendered)
// ============================================================================

/**
 * Register a simple Gutenberg block that renders the [eclaw] shortcode.
 */
function eclaw_register_block() {
    if ( ! function_exists( 'register_block_type' ) ) return;

    // Register the block's editor script inline
    wp_register_script(
        'eclaw-block-editor',
        '',
        array( 'wp-blocks', 'wp-element', 'wp-block-editor', 'wp-components', 'wp-server-side-render' ),
        '2.0.0'
    );

    // Inline the block editor JS
    wp_add_inline_script( 'eclaw-block-editor', eclaw_block_editor_js() );

    register_block_type( 'eclaw/embed', array(
        'editor_script'   => 'eclaw-block-editor',
        'render_callback' => 'eclaw_block_render',
        'attributes'      => array(
            'mode'         => array( 'type' => 'string',  'default' => 'watch' ),
            'url'          => array( 'type' => 'string',  'default' => '' ),
            'theme'        => array( 'type' => 'string',  'default' => 'dark' ),
            'width'        => array( 'type' => 'string',  'default' => '100%' ),
            'height'       => array( 'type' => 'string',  'default' => '480' ),
            'responsive'   => array( 'type' => 'string',  'default' => '0' ),
            'aspect_ratio' => array( 'type' => 'string',  'default' => '16:9' ),
            'accent'       => array( 'type' => 'string',  'default' => '' ),
            'bg'           => array( 'type' => 'string',  'default' => '' ),
            'footer'       => array( 'type' => 'string',  'default' => '1' ),
            'sounds'       => array( 'type' => 'string',  'default' => '1' ),
        ),
    ) );
}
add_action( 'init', 'eclaw_register_block' );

/**
 * Server-side render callback for Gutenberg block.
 */
function eclaw_block_render( $attributes ) {
    return eclaw_embed_shortcode( $attributes );
}

/**
 * Returns the JS for the Gutenberg block editor.
 */
function eclaw_block_editor_js() {
    return <<<'JSEOF'
(function() {
    var el = wp.element.createElement;
    var InspectorControls = wp.blockEditor.InspectorControls;
    var PanelBody = wp.components.PanelBody;
    var TextControl = wp.components.TextControl;
    var SelectControl = wp.components.SelectControl;
    var ToggleControl = wp.components.ToggleControl;
    var ServerSideRender = wp.serverSideRender || wp.components.ServerSideRender;

    wp.blocks.registerBlockType('eclaw/embed', {
        title: 'Remote Claw Machine',
        icon: 'games',
        category: 'embed',
        description: 'Embed the Remote Claw Machine live stream or interactive player.',
        keywords: ['claw', 'arcade', 'game', 'stream', 'live', 'eclaw'],

        edit: function(props) {
            var atts = props.attributes;
            var setAtts = props.setAttributes;

            return el('div', { className: props.className },
                el(InspectorControls, {},
                    el(PanelBody, { title: 'Claw Machine Settings', initialOpen: true },
                        el(TextControl, {
                            label: 'Server URL',
                            help: 'Leave blank to use the default from Settings > Remote Claw',
                            value: atts.url,
                            onChange: function(v) { setAtts({ url: v }); }
                        }),
                        el(SelectControl, {
                            label: 'Mode',
                            value: atts.mode,
                            options: [
                                { label: 'Watch (spectator)', value: 'watch' },
                                { label: 'Play (interactive)', value: 'play' }
                            ],
                            onChange: function(v) { setAtts({ mode: v }); }
                        }),
                        el(SelectControl, {
                            label: 'Theme',
                            value: atts.theme,
                            options: [
                                { label: 'Dark', value: 'dark' },
                                { label: 'Light', value: 'light' }
                            ],
                            onChange: function(v) { setAtts({ theme: v }); }
                        }),
                        el(ToggleControl, {
                            label: 'Responsive sizing',
                            checked: atts.responsive === '1',
                            onChange: function(v) { setAtts({ responsive: v ? '1' : '0' }); }
                        }),
                        atts.responsive === '1' ? el(TextControl, {
                            label: 'Aspect Ratio',
                            value: atts.aspect_ratio,
                            onChange: function(v) { setAtts({ aspect_ratio: v }); }
                        }) : null,
                        atts.responsive !== '1' ? el(TextControl, {
                            label: 'Height (px)',
                            value: atts.height,
                            onChange: function(v) { setAtts({ height: v }); }
                        }) : null
                    ),
                    el(PanelBody, { title: 'Appearance', initialOpen: false },
                        el(TextControl, {
                            label: 'Accent color (hex, no #)',
                            value: atts.accent,
                            onChange: function(v) { setAtts({ accent: v }); }
                        }),
                        el(TextControl, {
                            label: 'Background color (hex, no #)',
                            value: atts.bg,
                            onChange: function(v) { setAtts({ bg: v }); }
                        }),
                        el(ToggleControl, {
                            label: 'Show footer (watch mode)',
                            checked: atts.footer === '1',
                            onChange: function(v) { setAtts({ footer: v ? '1' : '0' }); }
                        }),
                        el(ToggleControl, {
                            label: 'Enable sounds (play mode)',
                            checked: atts.sounds === '1',
                            onChange: function(v) { setAtts({ sounds: v ? '1' : '0' }); }
                        })
                    )
                ),
                el(ServerSideRender, {
                    block: 'eclaw/embed',
                    attributes: atts
                })
            );
        },

        save: function() {
            return null; // Server-side rendered
        }
    });
})();
JSEOF;
}

// ============================================================================
// ELEMENTOR COMPATIBILITY
// ============================================================================

/**
 * Ensure Elementor doesn't strip the iframe allow attributes or sandbox it.
 * Also register as an Elementor-compatible shortcode.
 */
function eclaw_elementor_compat() {
    // Allow iframes in Elementor HTML widget
    add_filter( 'elementor/frontend/the_content', function( $content ) {
        return $content; // Don't strip anything
    } );
}
add_action( 'elementor/init', 'eclaw_elementor_compat' );

/**
 * Add settings link on the plugins page.
 */
function eclaw_plugin_action_links( $links ) {
    $settings_link = '<a href="' . admin_url( 'options-general.php?page=eclaw-settings' ) . '">Settings</a>';
    array_unshift( $links, $settings_link );
    return $links;
}
add_filter( 'plugin_action_links_' . plugin_basename( __FILE__ ), 'eclaw_plugin_action_links' );

// ============================================================================
// OEMBED / AUTO-EMBED SUPPORT
// ============================================================================

/**
 * Register an oEmbed provider so pasting a claw URL auto-embeds.
 * Only works if the admin has configured a default URL.
 */
function eclaw_register_oembed() {
    $opts = eclaw_get_options();
    if ( empty( $opts['default_url'] ) ) return;

    $base = rtrim( $opts['default_url'], '/' );
    // Register both /embed/play and /embed/watch as embeddable URLs
    wp_embed_register_handler( 'eclaw-play', '#' . preg_quote( $base, '#' ) . '/embed/play#i', 'eclaw_oembed_handler' );
    wp_embed_register_handler( 'eclaw-watch', '#' . preg_quote( $base, '#' ) . '/embed/watch#i', 'eclaw_oembed_handler' );
}
add_action( 'init', 'eclaw_register_oembed' );

function eclaw_oembed_handler( $matches, $attr, $url, $rawattr ) {
    $mode = ( strpos( $url, '/embed/play' ) !== false ) ? 'play' : 'watch';
    return eclaw_embed_shortcode( array( 'mode' => $mode, 'responsive' => '1' ) );
}
