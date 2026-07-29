<?php
/**
 * Plugin Name: موصل صحفي هب
 * Description: يربط موقع ووردبريس الخاص بك بنظام صحفي هب بسهولة ويدير إعداداته.
 * Version: 1.2.0
 * Author: صحفي هب
 */

if (!defined('ABSPATH')) {
    exit; // Exit if accessed directly
}

class Enshrly_Connector {
    public function __construct() {
        add_action('admin_menu', array($this, 'add_admin_menu'));
        add_action('admin_enqueue_scripts', array($this, 'enqueue_scripts'));
        add_action('admin_head', array($this, 'menu_icon_styles'));
        add_action('transition_post_status', array($this, 'on_post_published'), 10, 3);
    }

    /**
     * Fires on every post status change on the site, regardless of who/what
     * published it (a human editor in wp-admin, or Sahafi Hub's own REST API
     * push in connect_to_enshrly()) - this is the single source of truth for
     * "a news post just went live" that the Facebook auto-posting feature relies on.
     */
    public function on_post_published($new_status, $old_status, $post) {
        if ($new_status !== 'publish' || $old_status === 'publish' || $post->post_type !== 'post') {
            return;
        }

        $token = get_option('enshrly_token', '');
        $server_url = get_option('enshrly_server_url', '');
        if (empty($token) || empty($server_url)) {
            return;
        }

        $thumb_id = get_post_thumbnail_id($post->ID);
        $image_url = $thumb_id ? wp_get_attachment_image_url($thumb_id, 'large') : '';

        wp_remote_post(rtrim($server_url, '/') . '/api/wp-post-published/', array(
            'timeout' => 15,
            'blocking' => false,
            'headers' => array('Content-Type' => 'application/json'),
            'body' => wp_json_encode(array(
                'token' => $token,
                'site_url' => get_site_url(),
                'post_id' => $post->ID,
                'title' => $post->post_title,
                'excerpt' => wp_strip_all_tags(get_the_excerpt($post)),
                'link' => get_permalink($post->ID),
                'image_url' => $image_url,
                'categories' => wp_get_post_categories($post->ID, array('fields' => 'names')),
            )),
        ));
    }

    public function menu_icon_styles() {
        echo '<style>#adminmenu #toplevel_page_enshrly-connector .wp-menu-image img { width: 20px !important; height: 20px !important; padding: 6px 0 0 !important; }</style>';
    }

    public function enqueue_scripts($hook) {
        if ('toplevel_page_enshrly-connector' === $hook) {
            wp_enqueue_style('wp-color-picker');
            wp_enqueue_script('wp-color-picker');
            wp_add_inline_script('wp-color-picker', 'jQuery(document).ready(function($){ $(".color-picker").wpColorPicker(); });');
        }
    }

    public function add_admin_menu() {
        add_menu_page(
            'تكامل صحفي هب',
            'صحفي هب',
            'manage_options',
            'enshrly-connector',
            array($this, 'settings_page'),
            plugin_dir_url(__FILE__) . 'assets/icon.png',
            100
        );
    }

    public function settings_page() {
        if (!current_user_can('manage_options')) {
            return;
        }

        $message = '';
        $error = '';

        if (isset($_POST['enshrly_connect_nonce']) && wp_verify_nonce($_POST['enshrly_connect_nonce'], 'enshrly_connect_action')) {
            $token = sanitize_text_field($_POST['enshrly_token']);
            $server_url = esc_url_raw($_POST['enshrly_server_url']);

            if (empty($token) || empty($server_url)) {
                $error = 'الرجاء إدخال كود الربط ورابط النظام.';
            } else {
                // Save settings
                update_option('enshrly_server_url', $server_url);
                update_option('enshrly_token', $token);
                $seo_fields = [
                    'use_rich_formatting', 'use_internal_links', 'use_explainer_style'
                ];
                foreach ($seo_fields as $field) {
                    update_option('enshrly_' . $field, isset($_POST[$field]));
                }

                if (isset($_POST['heading_color'])) {
                    update_option('enshrly_heading_color', sanitize_hex_color($_POST['heading_color']));
                }
                if (isset($_POST['site_tags'])) {
                    update_option('enshrly_site_tags', sanitize_text_field($_POST['site_tags']));
                }

                $enabled_price_articles = isset($_POST['enabled_price_articles']) && is_array($_POST['enabled_price_articles']) ? array_map('sanitize_text_field', $_POST['enabled_price_articles']) : [];
                update_option('enshrly_enabled_price_articles', $enabled_price_articles);

                // Category Mapping
                $category_mapping = [];
                $secondary_by_key = [];
                foreach ($_POST as $k => $v) {
                    if (strpos($k, 'cat_') !== 0) continue;
                    if (substr($k, -10) === '_secondary') {
                        if (is_array($v)) {
                            $mapped_key = substr($k, 4, -10); // strips leading 'cat_' and trailing '_secondary'
                            $secondary_by_key[$mapped_key] = array_values(array_filter(array_map('intval', $v)));
                        }
                    } else {
                        $val = intval($v);
                        if ($val > 0) {
                            $mapped_key = substr($k, 4); // removes 'cat_'
                            $category_mapping[$mapped_key] = $val;
                        }
                    }
                }
                // Merge in any additional (secondary) categories as {"primary": X, "secondary": [Y, Z]}
                foreach ($secondary_by_key as $mapped_key => $secondary_ids) {
                    if (empty($secondary_ids)) continue;
                    $entry = array('secondary' => $secondary_ids);
                    if (!empty($category_mapping[$mapped_key])) {
                        $entry['primary'] = $category_mapping[$mapped_key];
                    }
                    $category_mapping[$mapped_key] = $entry;
                }
                update_option('enshrly_category_mapping', $category_mapping);

                // Source Groups
                $source_groups = isset($_POST['source_groups']) && is_array($_POST['source_groups']) ? array_map('intval', $_POST['source_groups']) : [];
                update_option('enshrly_source_groups', $source_groups);

                // Schedules
                $schedules = [];
                if (!empty($_POST['schedules_json'])) {
                    $decoded = json_decode(stripslashes($_POST['schedules_json']), true);
                    if (is_array($decoded)) {
                        $schedules = $decoded;
                    }
                }
                update_option('enshrly_schedules', $schedules);

                // Authors
                $wp_author_ids = isset($_POST['wp_author_ids']) && is_array($_POST['wp_author_ids']) ? implode(',', array_map('intval', $_POST['wp_author_ids'])) : '';
                update_option('enshrly_wp_author_ids', $wp_author_ids);

                $result = $this->connect_to_enshrly($token, $server_url, $category_mapping, $source_groups, $schedules, $wp_author_ids, $enabled_price_articles);
                if (is_wp_error($result)) {
                    $error = $result->get_error_message();
                } else {
                    $message = 'تم حفظ الإعدادات وربط الموقع بنجاح!';
                    update_option('enshrly_connected', true);
                }
            }
        }

        $is_connected = get_option('enshrly_connected', false);
        $saved_server_url = get_option('enshrly_server_url', '');
        $saved_token = get_option('enshrly_token', '');
        
        $categories = get_categories(['hide_empty' => false]);
        $saved_mapping = get_option('enshrly_category_mapping', []);
        $saved_source_groups = get_option('enshrly_source_groups', []);
        $saved_enabled_price_articles = get_option('enshrly_enabled_price_articles', []);
        $saved_schedules = get_option('enshrly_schedules', []);
        $saved_author_ids = get_option('enshrly_wp_author_ids', '');
        
        $wp_users = get_users(['who' => 'authors']);

        $plugin_data = ['source_groups' => [], 'content_types' => [], 'daily_limit' => 3];
        $fetch_error = '';
        
        if (empty($saved_token) || empty($saved_server_url)) {
            $fetch_error = 'يرجى إدخال رابط النظام وكود الربط والضغط على حفظ لتظهر خيارات "مجموعات المصادر" و"الجدولة".';
        } else {
            $api_endpoint = rtrim($saved_server_url, '/') . '/api/wp-plugin-data/?token=' . urlencode($saved_token);
            $response = wp_remote_get($api_endpoint, array('timeout' => 15));
            if (is_wp_error($response)) {
                $fetch_error = 'تعذر الاتصال بالخادم لجلب المجموعات والجداول. الخطأ: ' . $response->get_error_message();
            } else {
                $body = wp_remote_retrieve_body($response);
                $data = json_decode($body, true);
                if (isset($data['status']) && $data['status'] === 'success') {
                    $plugin_data = $data['data'];
                } else {
                    $fetch_error = 'رد غير متوقع من الخادم أو الكود غير صحيح.';
                }
            }
        }

        ?>
        <div class="wrap" style="max-width: 900px; margin-bottom: 50px;">
            <h1 style="margin-bottom: 20px; display: flex; align-items: center; gap: 10px;">
                <img src="<?php echo esc_url(plugin_dir_url(__FILE__) . 'assets/icon.png'); ?>" alt="صحفي هب" style="height: 36px; width: 36px;" />
                ربط وإعدادات نظام صحفي هب
            </h1>
            
            <?php if ($message): ?>
                <div class="notice notice-success is-dismissible"><p><?php echo esc_html($message); ?></p></div>
            <?php endif; ?>
            
            <?php if ($error): ?>
                <div class="notice notice-error is-dismissible"><p><?php echo esc_html($error); ?></p></div>
            <?php endif; ?>

            <?php if ($is_connected): ?>
                <div class="notice notice-info" style="border-right-color: #00a0d2; border-left: none;">
                    <p><strong><span class="dashicons dashicons-yes-alt" style="color: green;"></span> الموقع مرتبط بالنظام ويعمل.</strong> يمكنك تعديل الخيارات أدناه والضغط على "حفظ وتحديث الربط" ليتم إرسالها للنظام.</p>
                </div>
            <?php endif; ?>

            <form method="post" action="">
                <?php wp_nonce_field('enshrly_connect_action', 'enshrly_connect_nonce'); ?>
                
                <div class="postbox" style="padding: 20px; margin-top: 20px;">
                    <h2>1. بيانات الربط الأساسية</h2>
                    <table class="form-table">
                        <tr valign="top">
                            <th scope="row"><label for="enshrly_server_url">رابط نظام صحفي هب (Server URL)</label></th>
                            <td>
                                <input type="url" id="enshrly_server_url" name="enshrly_server_url" value="<?php echo esc_attr($saved_server_url); ?>" class="regular-text" required placeholder="https://your-enshrly-domain.com" />
                            </td>
                        </tr>
                        <tr valign="top">
                            <th scope="row"><label for="enshrly_token">كود الربط (Connection Token)</label></th>
                            <td>
                                <input type="text" id="enshrly_token" name="enshrly_token" value="<?php echo esc_attr($saved_token); ?>" class="regular-text" required autocomplete="off" />
                            </td>
                        </tr>
                        <tr valign="top">
                            <th scope="row"><label for="wp_author_ids">الكُتّاب (Authors)</label></th>
                            <td>
                                <?php
                                $saved_authors_array = !empty($saved_author_ids) ? explode(',', $saved_author_ids) : [];
                                ?>
                                <select id="wp_author_ids" name="wp_author_ids[]" multiple style="min-height: 100px; min-width: 200px;">
                                    <?php foreach ($wp_users as $u): ?>
                                        <option value="<?php echo esc_attr($u->ID); ?>" <?php echo in_array($u->ID, $saved_authors_array) ? 'selected' : ''; ?>><?php echo esc_html($u->display_name); ?></option>
                                    <?php endforeach; ?>
                                </select>
                                <p class="description">اختر كاتب أو أكثر لتوزيع الأخبار بينهم. (اضغط Ctrl أو Cmd للاختيار المتعدد)</p>
                            </td>
                        </tr>
                    </table>
                </div>

                <div class="postbox" style="padding: 20px; margin-top: 20px;">
                    <h2>2. خيارات السيو والصياغة (SEO)</h2>
                    <table class="form-table">
                        <tr valign="top">
                            <th scope="row">تنسيق غني وملوّن (H2/H3)</th>
                            <td>
                                <label><input type="checkbox" name="use_rich_formatting" value="1" <?php checked(get_option('enshrly_use_rich_formatting', false)); ?> /> تقسيم الخبر إلى عناوين فرعية H2/H3</label>
                            </td>
                        </tr>
                        <tr valign="top">
                            <th scope="row"><label for="heading_color">لون العناوين الفرعية</label></th>
                            <td>
                                <input type="text" id="heading_color" name="heading_color" value="<?php echo esc_attr(get_option('enshrly_heading_color', '#0066cc')); ?>" class="color-picker" data-default-color="#0066cc" />
                            </td>
                        </tr>
                        <tr valign="top">
                            <th scope="row">روابط داخلية</th>
                            <td>
                                <label><input type="checkbox" name="use_internal_links" value="1" <?php checked(get_option('enshrly_use_internal_links', false)); ?> /> تضمين روابط داخلية تلقائية</label>
                            </td>
                        </tr>
                        <tr valign="top">
                            <th scope="row">الأسلوب التفسيري (Explainer)</th>
                            <td>
                                <label><input type="checkbox" name="use_explainer_style" value="1" <?php checked(get_option('enshrly_use_explainer_style', false)); ?> /> استخدام أسلوب (س/ج) للأخبار التحليلية</label>
                            </td>
                        </tr>
                        <tr valign="top">
                            <th scope="row"><label for="site_tags">وسوم ثابتة (Tags)</label></th>
                            <td>
                                <input type="text" id="site_tags" name="site_tags" value="<?php echo esc_attr(get_option('enshrly_site_tags', '')); ?>" class="large-text" placeholder="مثال: أخبار اليوم, عاجل" />
                                <p class="description">افصل بينها بفاصلة. ستُضاف لكل خبر.</p>
                            </td>
                        </tr>
                    </table>
                </div>

                <div class="postbox" style="padding: 20px; margin-top: 20px;">
                    <h2>3. توجيه الأقسام (Category Mapping)</h2>
                    <p class="description">حدد القسم الأساسي (وأي أقسام إضافية إن أردت) في موقعك الذي سيتم نشر كل نوع من الأخبار فيه.</p>
                    <table class="form-table">
                        <tbody id="dynamic-category-mapping"></tbody>
                    </table>
                    <p class="description">تظهر هنا فقط الأقسام المطابقة للأسعار والمجموعات التي حددتها أعلاه.</p>
                </div>

                <?php if ($fetch_error): ?>
                    <div class="notice notice-warning inline"><p><strong>ملاحظة:</strong> <?php echo esc_html($fetch_error); ?></p></div>
                <?php endif; ?>

                <?php if (!empty($plugin_data['source_groups'])): ?>
                <div class="postbox" style="padding: 20px; margin-top: 20px;">
                    <h2>4. مجموعات المصادر المفضلة</h2>
                    <p class="description">حدد مجموعات المصادر التي تريد استلام الأخبار منها.</p>
                    <table class="form-table">
                        <tr valign="top">
                            <td colspan="2">
                                <?php
                                $all_source_groups = $plugin_data['source_groups'];
                                $top_level_groups = array_filter($all_source_groups, function($g) { return empty($g['parent_id']); });
                                foreach ($top_level_groups as $parent_group):
                                    $child_groups = array_filter($all_source_groups, function($g) use ($parent_group) {
                                        return !empty($g['parent_id']) && $g['parent_id'] == $parent_group['id'];
                                    });
                                ?>
                                    <div style="margin-bottom:15px;">
                                        <label style="display:block; font-weight:600; margin-bottom:6px;">
                                            <input type="checkbox" name="source_groups[]" value="<?php echo esc_attr($parent_group['id']); ?>" <?php checked(in_array($parent_group['id'], $saved_source_groups)); ?> />
                                            <?php echo esc_html($parent_group['name']); ?>
                                        </label>
                                        <?php if (!empty($child_groups)): ?>
                                            <div style="margin-right:28px;">
                                                <?php foreach ($child_groups as $child_group): ?>
                                                    <label style="display:inline-block; margin-left:20px; margin-bottom:8px;">
                                                        <input type="checkbox" name="source_groups[]" value="<?php echo esc_attr($child_group['id']); ?>" <?php checked(in_array($child_group['id'], $saved_source_groups)); ?> />
                                                        <?php echo esc_html($child_group['name']); ?>
                                                    </label>
                                                <?php endforeach; ?>
                                            </div>
                                        <?php endif; ?>
                                        <?php
                                        $price_group = null;
                                        foreach ($child_groups as $g) {
                                            if (!empty($g['is_price_articles_group'])) { $price_group = $g; break; }
                                        }
                                        if (!$price_group && !empty($parent_group['is_price_articles_group'])) { $price_group = $parent_group; }
                                        ?>
                                        <?php if ($price_group && !empty($plugin_data['content_types'])): ?>
                                            <div style="margin-right:48px; margin-top:6px; padding-right:12px; border-right:3px solid #ccc;">
                                                <p class="description" style="margin:4px 0;">حدد أسعار "<?php echo esc_html($price_group['name']); ?>" التي تريد توليدها ونشرها أوتوماتيكياً بالتفصيل:</p>
                                                <?php foreach ($plugin_data['content_types'] as $ct): if ($ct['id'] === 'regular') continue; ?>
                                                    <label style="display:inline-block; margin-left:20px; margin-bottom:8px;">
                                                        <input type="checkbox" name="enabled_price_articles[]" value="<?php echo esc_attr($ct['id']); ?>" <?php checked(in_array($ct['id'], $saved_enabled_price_articles)); ?> />
                                                        <?php echo esc_html($ct['name']); ?>
                                                    </label>
                                                <?php endforeach; ?>
                                            </div>
                                        <?php endif; ?>
                                    </div>
                                <?php endforeach; ?>
                            </td>
                        </tr>
                    </table>
                </div>
                <?php endif; ?>

                <?php if (!empty($plugin_data['content_types'])): ?>
                <div class="postbox" style="padding: 20px; margin-top: 20px;">
                    <h2>5. جدولة النشر (Schedules)</h2>
                    <p class="description">حدد أوقات النشر الآلي وأنواع المحتوى المطلوبة في كل فترة.</p>
                    <div id="enshrly-schedules-container"></div>
                    <button type="button" class="button" id="add-schedule-btn" style="margin-top: 10px;">+ إضافة فترة نشر</button>
                    <input type="hidden" name="schedules_json" id="schedules_json" value="<?php echo esc_attr(json_encode($saved_schedules)); ?>" />
                    
                    <script>
                    jQuery(document).ready(function($) {
                        var contentTypes = <?php echo json_encode($plugin_data['content_types']); ?>;
                        var savedSchedules = <?php echo json_encode($saved_schedules); ?>;
                        var dailyLimit = <?php echo intval($plugin_data['daily_limit'] ?? 3); ?>;
                        var savedMapping = <?php echo json_encode($saved_mapping); ?>;
                        var wpCategories = <?php echo json_encode(array_map(function($c) { return ['id' => $c->term_id, 'name' => $c->name]; }, $categories)); ?>;
                        var container = $('#enshrly-schedules-container');

                        // Clean 'regular' from static types
                        contentTypes = contentTypes.filter(function(ct) { return ct.id !== 'regular'; });

                        function getDynamicContentTypes() {
                            var enabledPriceIds = [];
                            $('input[name="enabled_price_articles[]"]:checked').each(function() { enabledPriceIds.push($(this).val()); });
                            var cts = contentTypes.filter(function(ct) { return enabledPriceIds.indexOf(ct.id) !== -1; });
                            $('input[name="source_groups[]"]:checked').each(function() {
                                var groupId = $(this).val();
                                var groupName = $(this).parent().text().trim();
                                cts.push({ id: 'group_' + groupId, name: 'أخبار ' + groupName });
                            });
                            return cts;
                        }

                        function renderCategoryMapping() {
                            var tbody = $('#dynamic-category-mapping');
                            tbody.empty();

                            var enabledPriceIds = [];
                            $('input[name="enabled_price_articles[]"]:checked').each(function() { enabledPriceIds.push($(this).val()); });

                            contentTypes.forEach(function(ct) {
                                if (enabledPriceIds.indexOf(ct.id) === -1) return;

                                var mappingVal = savedMapping[ct.id] || 0;
                                var primarySelected = 0, secondarySelected = [];
                                if (mappingVal && typeof mappingVal === 'object') {
                                    primarySelected = parseInt(mappingVal.primary || 0, 10);
                                    secondarySelected = (mappingVal.secondary || []).map(function(v) { return parseInt(v, 10); });
                                } else {
                                    primarySelected = parseInt(mappingVal || 0, 10);
                                }

                                var html = '<tr valign="top"><th scope="row"><label>' + ct.name + '</label></th><td>';
                                html += '<select name="cat_' + ct.id + '"><option value="0">-- اختر القسم الأساسي --</option>';
                                wpCategories.forEach(function(c) {
                                    var isSelected = (primarySelected === c.id) ? 'selected' : '';
                                    html += '<option value="' + c.id + '" ' + isSelected + '>' + c.name + '</option>';
                                });
                                html += '</select>';
                                html += '<div style="margin-top:8px;"><p class="description" style="margin:4px 0;">أقسام إضافية (اختياري، يُنشر الخبر فيها أيضاً):</p>';
                                wpCategories.forEach(function(c) {
                                    var checked = (secondarySelected.indexOf(c.id) !== -1) ? 'checked' : '';
                                    html += '<label style="display:inline-block; margin-left:15px; margin-bottom:6px;"><input type="checkbox" name="cat_' + ct.id + '_secondary[]" value="' + c.id + '" ' + checked + ' /> ' + c.name + '</label>';
                                });
                                html += '</div></td></tr>';
                                tbody.append(html);
                            });

                            $('input[name="source_groups[]"]:checked').each(function() {
                                var groupId = $(this).val();
                                var groupName = $(this).parent().text().trim();
                                var mappingKey = 'group_' + groupId;
                                var selected = savedMapping[mappingKey] || 0;
                                
                                var html = '<tr valign="top"><th scope="row"><label>قسم: أخبار ' + groupName + '</label></th><td>';
                                html += '<select name="cat_' + mappingKey + '"><option value="0">-- اختر القسم --</option>';
                                wpCategories.forEach(function(c) {
                                    var isSelected = (selected == c.id) ? 'selected' : '';
                                    html += '<option value="' + c.id + '" ' + isSelected + '>' + c.name + '</option>';
                                });
                                html += '</select></td></tr>';
                                tbody.append(html);
                            });
                        }

                        function updateContentTypesInSchedules() {
                            var dynamicCts = getDynamicContentTypes();
                            $('.schedule-row').each(function() {
                                var row = $(this);
                                var ctsContainer = row.find('.cts-container');
                                var checkedVals = [];
                                ctsContainer.find('.sched-ct:checked').each(function() { checkedVals.push($(this).val()); });
                                
                                var html = '';
                                dynamicCts.forEach(function(ct) {
                                    var checked = checkedVals.indexOf(ct.id) !== -1 ? 'checked' : '';
                                    html += '<label style="display:inline-block; margin-left:15px;"><input type="checkbox" class="sched-ct" value="' + ct.id + '" ' + checked + ' /> ' + ct.name + '</label>';
                                });
                                ctsContainer.html(html);
                            });
                        }

                        $('input[name="source_groups[]"]').on('change', function() {
                            renderCategoryMapping();
                            updateContentTypesInSchedules();
                        });

                        $('input[name="enabled_price_articles[]"]').on('change', function() {
                            renderCategoryMapping();
                            updateContentTypesInSchedules();
                        });

                        function renderSchedule(schedule) {
                            var html = '<div class="schedule-row" style="background:#f9f9f9; padding:15px; margin-bottom:10px; border:1px solid #ccc; position:relative;">';
                            html += '<button type="button" class="button remove-schedule" style="position:absolute; top:10px; left:10px; color:red;">حذف</button>';
                            html += '<label><strong>الوقت:</strong> <input type="time" class="sched-time" value="' + (schedule.time_of_day || '00:00') + '" required /></label><br><br>';
                            
                            html += '<strong>أنواع المحتوى:</strong><br>';
                            html += '<div class="cts-container"></div>';
                            
                            html += '<br><br><label><strong>عدد الأخبار للمجموعات:</strong> <input type="number" class="sched-count" min="1" max="20" value="' + (schedule.regular_news_count || 1) + '" style="width:60px;" /></label>';
                            html += '</div>';
                            
                            var el = $(html);
                            container.append(el);
                            
                            // populate checkboxes
                            var dynamicCts = getDynamicContentTypes();
                            var ctsContainer = el.find('.cts-container');
                            var h = '';
                            dynamicCts.forEach(function(ct) {
                                var checked = (schedule.content_types && schedule.content_types.indexOf(ct.id) !== -1) ? 'checked' : '';
                                h += '<label style="display:inline-block; margin-left:15px;"><input type="checkbox" class="sched-ct" value="' + ct.id + '" ' + checked + ' /> ' + ct.name + '</label>';
                            });
                            ctsContainer.html(h);
                        }

                        if (savedSchedules && savedSchedules.length > 0) {
                            savedSchedules.forEach(function(s) { renderSchedule(s); });
                        }

                        $('#add-schedule-btn').on('click', function() {
                            renderSchedule({});
                        });

                        container.on('click', '.remove-schedule', function() {
                            $(this).closest('.schedule-row').remove();
                        });

                        $('form').on('submit', function(e) {
                            var finalSchedules = [];
                            var totalGroupNews = 0;
                            $('.schedule-row').each(function() {
                                var row = $(this);
                                var time = row.find('.sched-time').val();
                                var count = parseInt(row.find('.sched-count').val()) || 0;
                                var cts = [];
                                var hasGroupNews = false;
                                row.find('.sched-ct:checked').each(function() {
                                    var v = $(this).val();
                                    cts.push(v);
                                    if (v.indexOf('group_') === 0) {
                                        hasGroupNews = true;
                                    }
                                });
                                finalSchedules.push({
                                    time_of_day: time,
                                    regular_news_count: count,
                                    content_types: cts
                                });
                                if (hasGroupNews) {
                                    totalGroupNews += count;
                                }
                            });
                            
                            if (totalGroupNews > dailyLimit) {
                                e.preventDefault();
                                alert('عفواً، لا يمكن حفظ الإعدادات. مجموع أخبار المجموعات المجدولة (' + totalGroupNews + ') يتجاوز الحد الأقصى اليومي المسموح لباقة موقعك وهو (' + dailyLimit + '). يرجى تقليل العدد والمحاولة مرة أخرى.');
                                return false;
                            }
                            
                            $('#schedules_json').val(JSON.stringify(finalSchedules));
                        });
                        
                        // Initial render
                        renderCategoryMapping();
                    });
                    </script>
                </div>
                <?php endif; ?>

                <?php submit_button('حفظ وتحديث الربط', 'primary', 'submit', true, ['style' => 'font-size: 16px; padding: 10px 30px; height: auto;']); ?>
            </form>
        </div>
        <?php
    }

    private function connect_to_enshrly($token, $server_url, $category_mapping, $source_groups, $schedules, $wp_author_ids, $enabled_price_articles = []) {
        if (!class_exists('WP_Application_Passwords')) {
            return new WP_Error('app_passwords_missing', 'خاصية Application Passwords غير متوفرة.');
        }

        $user_id = get_current_user_id();
        $user = wp_get_current_user();
        
        $app_password_name = 'تكامل صحفي هب ' . time();
        $generated = WP_Application_Passwords::create_new_application_password($user_id, array('name' => $app_password_name));
        
        if (is_wp_error($generated)) {
            return $generated;
        }

        list($new_password, $new_password_item) = $generated;

        $server_url = rtrim($server_url, '/');
        $api_endpoint = $server_url . '/api/wp-connect/';

        $payload = array(
            'token' => $token,
            'site_url' => get_site_url(),
            'username' => $user->user_login,
            'application_password' => $new_password,
            'enabled_price_articles' => $enabled_price_articles,
            'settings' => array(
                'use_rich_formatting' => get_option('enshrly_use_rich_formatting'),
                'heading_color' => get_option('enshrly_heading_color'),
                'use_internal_links' => get_option('enshrly_use_internal_links'),
                'use_explainer_style' => get_option('enshrly_use_explainer_style'),
                'site_tags' => get_option('enshrly_site_tags'),
                'category_mapping' => wp_json_encode($category_mapping),
                'source_groups' => $source_groups,
                'schedules' => $schedules,
                'wp_author_ids' => $wp_author_ids
            )
        );

        $response = wp_remote_post($api_endpoint, array(
            'method' => 'POST',
            'timeout' => 30,
            'headers' => array('Content-Type' => 'application/json'),
            'body' => wp_json_encode($payload)
        ));

        if (is_wp_error($response)) {
            return new WP_Error('api_error', 'حدث خطأ أثناء الاتصال بالنظام: ' . $response->get_error_message());
        }

        $body = wp_remote_retrieve_body($response);
        $data = json_decode($body, true);
        $status_code = wp_remote_retrieve_response_code($response);

        if ($status_code !== 200 || empty($data) || (isset($data['status']) && $data['status'] !== 'success')) {
            $error_msg = isset($data['message']) ? $data['message'] : 'فشل الربط.';
            return new WP_Error('api_error', $error_msg);
        }

        return true;
    }
}

new Enshrly_Connector();
