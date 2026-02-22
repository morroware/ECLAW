<?php
/**
 * Plugin Name: Remote Claw Embed
 * Description: Embed the Remote Claw Machine stream or player on any page via [eclaw] shortcode.
 * Version:     1.0.0
 * Author:      The Castle Fun Center
 *
 * Installation: Copy this file to wp-content/mu-plugins/eclaw-embed.php
 * (or add it as a regular plugin in wp-content/plugins/eclaw-embed/eclaw-embed.php)
 *
 * Usage:
 *   Watch-only:  [eclaw url="https://claw.yourdomain.com"]
 *   Interactive:  [eclaw mode="play" url="https://claw.yourdomain.com"]
 *   Custom size:  [eclaw url="https://claw.yourdomain.com" width="100%" height="600"]
 *   Light theme:  [eclaw url="https://claw.yourdomain.com" theme="light"]
 */

function eclaw_embed_shortcode( $atts ) {
    $atts = shortcode_atts( array(
        'mode'   => 'watch',    // 'watch' or 'play'
        'url'    => '',         // Base URL of the ECLAW server (required)
        'width'  => '100%',
        'height' => '480',
        'theme'  => 'dark',     // 'dark' or 'light'
        'footer' => '1',        // '0' to hide footer (watch mode only)
        'sounds' => '1',        // '0' to mute sounds (play mode)
        'accent' => '',         // Hex accent color without # (e.g. 'ef4444')
    ), $atts, 'eclaw' );

    if ( empty( $atts['url'] ) ) {
        return '<p style="color:red;"><strong>[eclaw]</strong> Error: <code>url</code> attribute is required. Example: <code>[eclaw url="https://claw.yourdomain.com"]</code></p>';
    }

    $embed_path = ( $atts['mode'] === 'play' ) ? '/embed/play' : '/embed/watch';

    $query_params = array();
    if ( $atts['theme'] !== 'dark' )  $query_params['theme']  = $atts['theme'];
    if ( $atts['footer'] === '0' )    $query_params['footer'] = '0';
    if ( $atts['sounds'] === '0' )    $query_params['sounds'] = '0';
    if ( ! empty( $atts['accent'] ) ) $query_params['accent'] = $atts['accent'];

    $src = rtrim( $atts['url'], '/' ) . $embed_path;
    if ( ! empty( $query_params ) ) {
        $src .= '?' . http_build_query( $query_params );
    }

    return sprintf(
        '<iframe src="%s" width="%s" height="%s" frameborder="0" ' .
        'allow="autoplay; encrypted-media" allowfullscreen ' .
        'style="border:0; border-radius:8px; max-width:100%%;" ' .
        'loading="lazy" title="Remote Claw Machine"></iframe>',
        esc_url( $src ),
        esc_attr( $atts['width'] ),
        esc_attr( $atts['height'] )
    );
}
add_shortcode( 'eclaw', 'eclaw_embed_shortcode' );
