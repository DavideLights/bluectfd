// BlueAgent Admin Dashboard Controller
(function () {
    function init() {
        const $ = window.$ || (window.CTFd && window.CTFd.lib && window.CTFd.lib.$);
        if (!$) {
            setTimeout(init, 30);
            return;
        }

        $(function () {
            function getNonce() {
                return (window.init && window.init.csrfNonce) ||
                       (window.CTFd && window.CTFd.config && window.CTFd.config.csrfNonce) ||
                       $("input[name='nonce']").val() || "";
            }

            async function safeFetchJson(url, options = {}) {
                const nonce = getNonce();
                options.headers = options.headers || {};
                if (!options.headers["CSRF-Token"] && nonce) {
                    options.headers["CSRF-Token"] = nonce;
                }
                const res = await fetch(url, options);
                const text = await res.text();
                let data;
                try {
                    data = JSON.parse(text);
                } catch (err) {
                    throw new Error(`Server returned HTTP ${res.status}: ${text.slice(0, 150)}`);
                }
                return data;
            }

            loadOverview();
            loadMachines();
            loadImages();
            loadLogs();

            $("#btn-refresh-overview").on("click", function () {
                loadOverview();
                loadMachines();
                loadImages();
                loadLogs();
            });

            $("#btn-refresh-logs").on("click", loadLogs);

    // ==========================================
    // 1. Overview & Teams
    // ==========================================
    function loadOverview() {
        fetch("/admin/plugins/blueagent/api/overview")
            .then(res => res.json())
            .then(data => {
                if (!data.success) return;

                // Health Badge
                const badge = $("#ba-health-badge");
                if (data.health && data.health.online) {
                    badge.removeClass("badge-secondary badge-danger").addClass("badge-success").text("ONLINE: v" + data.health.version);
                } else {
                    badge.removeClass("badge-secondary badge-success").addClass("badge-danger").text("OFFLINE: Unreachable");
                }

                // Populate settings fields
                if (data.settings) {
                    $("#setting-url").val(data.settings.blueagent_url || "");
                    $("#setting-cooldown").val(data.settings.verify_cooldown || 30);
                }

                // Populate challenge select in bulk deploy modal
                const chalSelect = $("#deploy-challenge-select");
                chalSelect.empty();
                (data.challenges || []).forEach(c => {
                    chalSelect.append($("<option>").val(c.id).text(`${c.name} (ID ${c.id})`));
                });

                // Populate teams table
                const tbody = $("#table-teams-overview tbody");
                tbody.empty();

                if (!data.accounts || data.accounts.length === 0) {
                    tbody.append($("<tr><td colspan='6' class='text-center text-muted'>No accounts found.</td></tr>"));
                    return;
                }

                data.accounts.forEach(acc => {
                    const tr = $("<tr>");
                    tr.append($("<td>").addClass("font-weight-bold").text(acc.name));
                    tr.append($("<td>").text(acc.type.toUpperCase()));
                    tr.append($("<td>").html(acc.team_k !== null ? `<code>${acc.team_k}</code>` : "<span class='text-muted'>Not Allocated</span>"));
                    tr.append($("<td>").html(acc.team_k !== null ? `<code>10.${acc.team_k}.1.0/24</code>` : "-"));

                    const statusBadge = acc.provisioned
                        ? "<span class='badge badge-success'>Provisioned</span>"
                        : "<span class='badge badge-secondary'>Not Provisioned</span>";
                    tr.append($("<td>").html(statusBadge));

                    // Actions
                    const actionsTd = $("<td>");
                    if (!acc.provisioned) {
                        const btnProv = $("<button>")
                            .addClass("btn btn-sm btn-outline-primary mr-1 btn-prov-team")
                            .attr("data-id", acc.account_id)
                            .text("Provision");
                        actionsTd.append(btnProv);
                    } else {
                        const btnPeers = $("<button>")
                            .addClass("btn btn-sm btn-info mr-1 btn-view-peers")
                            .attr("data-k", acc.team_k)
                            .text("Telemetry");
                        const btnReset = $("<button>")
                            .addClass("btn btn-sm btn-warning mr-1 btn-reset-wg")
                            .attr("data-k", acc.team_k)
                            .text("Reset WG");
                        const btnDelete = $("<button>")
                            .addClass("btn btn-sm btn-danger btn-purge-team")
                            .attr("data-k", acc.team_k)
                            .text("Purge");

                        actionsTd.append(btnPeers).append(btnReset).append(btnDelete);
                    }

                    tr.append(actionsTd);
                    tbody.append(tr);
                });
            })
            .catch(err => console.error("Error loading overview:", err));
    }

    // Provision team bridge & WireGuard
    $(document).on("click", ".btn-prov-team", function () {
        const accId = $(this).attr("data-id");
        $(this).prop("disabled", true).text("Provisioning...");

        safeFetchJson(`/admin/plugins/blueagent/api/teams/${accId}/provision`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ nonce: getNonce() })
        })
        .then(data => {
            if (data.success) {
                loadOverview();
            } else {
                alert("Provisioning failed: " + data.message);
                loadOverview();
            }
        })
        .catch(err => {
            alert("Error: " + err.message);
            loadOverview();
        });
    });

    // View peer telemetry modal
    $(document).on("click", ".btn-view-peers", function () {
        const teamK = $(this).attr("data-k");
        $("#modal-peer-team-k").text(`Team ${teamK}`);
        const tbody = $("#modal-peer-table tbody");
        tbody.html("<tr><td colspan='6' class='text-center text-muted'>Fetching telemetry...</td></tr>");
        $("#modal-peer-telemetry").modal("show");

        fetch(`/admin/plugins/blueagent/api/teams/${teamK}/peers`)
            .then(res => res.json())
            .then(data => {
                tbody.empty();
                if (data.success && data.peers && data.peers.length > 0) {
                    data.peers.forEach(p => {
                        const tr = $("<tr>");
                        tr.append($("<td>").text(p.name));
                        tr.append($("<td>").html(`<code>${p.client_ip}</code>`));
                        tr.append($("<td>").html(`<small><code>${p.public_key.substring(0, 12)}...</code></small>`));
                        tr.append($("<td>").text(p.latest_handshake ? `${p.latest_handshake}s ago` : "Never"));
                        tr.append($("<td>").text(`${(p.transfer_rx / 1024).toFixed(1)}KB / ${(p.transfer_tx / 1024).toFixed(1)}KB`));
                        tr.append($("<td>").html(p.online ? "<span class='badge badge-success'>ONLINE</span>" : "<span class='badge badge-secondary'>OFFLINE</span>"));
                        tbody.append(tr);
                    });
                } else {
                    tbody.html("<tr><td colspan='6' class='text-center text-muted'>No active WireGuard peers for this team.</td></tr>");
                }
            })
            .catch(err => {
                tbody.html(`<tr><td colspan='6' class='text-center text-danger'>Error loading peers: ${err}</td></tr>`);
            });
    });

    // Reset WireGuard peers
    $(document).on("click", ".btn-reset-wg", function () {
        const teamK = $(this).attr("data-k");
        if (!confirm(`Are you sure you want to reset all WireGuard peers for Team ${teamK}? Connected players will need to re-download their config.`)) return;

        safeFetchJson(`/admin/plugins/blueagent/api/teams/${teamK}/reset-wg`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ nonce: getNonce() })
        })
        .then(data => {
            alert(data.message || (data.success ? "WireGuard reset complete." : "Reset failed."));
        })
        .catch(err => {
            alert("Error: " + err.message);
        });
    });

    // Purge team environment
    $(document).on("click", ".btn-purge-team", function () {
        const teamK = $(this).attr("data-k");
        if (!confirm(`WARNING: Completely purge Team ${teamK}? This destroys all live containers, interfaces, and frees the team_k index.`)) return;

        safeFetchJson(`/admin/plugins/blueagent/api/teams/${teamK}`, {
            method: "DELETE",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ nonce: getNonce() })
        })
        .then(data => {
            alert(data.message || "Team purged.");
            loadOverview();
            loadMachines();
        })
        .catch(err => {
            alert("Error: " + err.message);
        });
    });

    // ==========================================
    // 2. Active Machines
    // ==========================================
    function loadMachines() {
        fetch("/admin/plugins/blueagent/api/machines")
            .then(res => res.json())
            .then(data => {
                const tbody = $("#table-machines-overview tbody");
                tbody.empty();
                if (data.success && data.machines && data.machines.length > 0) {
                    data.machines.forEach(m => {
                        const tr = $("<tr>");
                        tr.append($("<td>").text(m.team_k));
                        tr.append($("<td>").text(m.spec_name));
                        tr.append($("<td>").html(`<code>${m.instance_name}</code>`));
                        tr.append($("<td>").html(`<code>${m.ip_address}</code>`));
                        tr.append($("<td>").text(m.image_alias));
                        tbody.append(tr);
                    });
                } else {
                    tbody.html("<tr><td colspan='5' class='text-center text-muted'>No running container instances detected across teams.</td></tr>");
                }
            })
            .catch(() => {});
    }

    // Bulk deploy challenge modal submit
    $("#form-bulk-deploy").on("submit", function (e) {
        e.preventDefault();
        const chalId = $("#deploy-challenge-select").val();
        const target = $("#deploy-targets-select").val();
        const $btn = $("#btn-submit-bulk-deploy");
        const resultsDiv = $("#bulk-deploy-results");

        $btn.prop("disabled", true).text("Deploying...");
        resultsDiv.show().html("<div class='alert alert-info'>Dispatching container deployments across teams...</div>");

        safeFetchJson(`/admin/plugins/blueagent/api/challenges/${chalId}/deploy`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ team_ks: target, nonce: getNonce() })
        })
        .then(data => {
            $btn.prop("disabled", false).text("Start Deployment");
            if (data.success) {
                let html = "<div class='alert alert-success'>Bulk deployment dispatched:<ul>";
                (data.results || []).forEach(r => {
                    html += `<li>Team ${r.team_k}: ${r.success ? "✓ " + r.status : "✗ " + r.error}</li>`;
                });
                html += "</ul></div>";
                resultsDiv.html(html);
                loadMachines();
            } else {
                resultsDiv.html(`<div class='alert alert-danger'>Error: ${data.message}</div>`);
            }
        })
        .catch(err => {
            $btn.prop("disabled", false).text("Start Deployment");
            resultsDiv.html(`<div class='alert alert-danger'>Network error: ${err.message}</div>`);
        });
    });

    // ==========================================
    // 3. Image Catalog
    // ==========================================
    function loadImages() {
        fetch("/admin/plugins/blueagent/api/images")
            .then(res => res.json())
            .then(data => {
                const tbody = $("#table-images-overview tbody");
                tbody.empty();
                if (data.success && data.images && data.images.length > 0) {
                    data.images.forEach(img => {
                        const tr = $("<tr>");
                        tr.append($("<td>").addClass("font-weight-bold").text(img.alias));

                        let statusBadge = "<span class='badge badge-secondary'>" + img.status + "</span>";
                        if (img.status === "ready") statusBadge = "<span class='badge badge-success'>READY</span>";
                        if (img.status === "building") statusBadge = "<span class='badge badge-warning'>BUILDING...</span>";
                        if (img.status === "failed") statusBadge = "<span class='badge badge-danger'>FAILED</span>";
                        tr.append($("<td>").html(statusBadge));

                        tr.append($("<td>").html(img.fingerprint ? `<small><code>${img.fingerprint.substring(0, 12)}...</code></small>` : "-"));
                        tr.append($("<td>").text(img.created_at ? img.created_at.substring(0, 19).replace("T", " ") : "-"));

                        const btnLogs = $("<button>")
                            .addClass("btn btn-sm btn-outline-info mr-1 btn-view-image-logs")
                            .attr("data-alias", img.alias)
                            .text("Logs");
                        const btnDel = $("<button>")
                            .addClass("btn btn-sm btn-outline-danger btn-delete-image")
                            .attr("data-alias", img.alias)
                            .text("Delete");
                        tr.append($("<td>").append(btnLogs).append(btnDel));

                        tbody.append(tr);
                    });
                } else {
                    tbody.html("<tr><td colspan='5' class='text-center text-muted'>No Distrobuilder images registered.</td></tr>");
                }
            })
            .catch(() => {});
    }

    // Build Image Form Submit
    $("#form-build-image").on("submit", function (e) {
        e.preventDefault();
        const formData = new FormData(this);
        const nonce = getNonce();
        if (!formData.has("nonce") && nonce) {
            formData.append("nonce", nonce);
        }

        const fileInput = document.getElementById("image-file-input");
        const yamlText = ($("#image-text-input").val() || "").trim();
        if ((!fileInput || !fileInput.files || fileInput.files.length === 0) && !yamlText) {
            alert("Please select a Distrobuilder file or paste YAML definition content.");
            return;
        }

        const $btn = $("#btn-submit-build-image");
        $btn.prop("disabled", true).text("Submitting Build...");

        safeFetchJson("/admin/plugins/blueagent/api/images", {
            method: "POST",
            body: formData
        })
        .then(data => {
            $btn.prop("disabled", false).text("Submit Build");
            if (data.success) {
                alert("Build initiated successfully for alias: " + (data.image ? data.image.alias : ""));
                $("#modal-build-image").modal("hide");
                loadImages();
            } else {
                alert("Failed to initiate image build: " + data.message);
            }
        })
        .catch(err => {
            $btn.prop("disabled", false).text("Submit Build");
            alert("Build Error: " + err.message);
        });
    });

    // Delete image
    $(document).on("click", ".btn-delete-image", function () {
        const alias = $(this).attr("data-alias");
        if (!confirm(`Delete image alias '${alias}' from Incus and BlueAgent catalog?`)) return;

        safeFetchJson(`/admin/plugins/blueagent/api/images/${alias}`, {
            method: "DELETE",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ nonce: getNonce() })
        })
        .then(data => {
            alert(data.message || (data.success ? "Image deleted." : "Delete failed."));
            loadImages();
        })
        .catch(err => {
            alert("Delete error: " + err.message);
        });
    });

    // View Image Build Logs
    $(document).on("click", ".btn-view-image-logs", function () {
        const alias = $(this).attr("data-alias");
        $("#modal-image-logs-alias").text(alias);
        const pre = $("#modal-image-logs-content");
        pre.text("Fetching compilation logs...");
        $("#modal-image-logs").modal("show");

        fetch(`/admin/plugins/blueagent/api/images/${alias}/logs`)
            .then(res => res.text())
            .then(text => {
                pre.text(text || "No logs available for this build.");
            })
            .catch(err => {
                pre.text("Error fetching logs: " + err);
            });
    });

    // ==========================================
    // 4. Verification History Logs
    // ==========================================
    function loadLogs() {
        fetch("/admin/plugins/blueagent/api/verify-log?limit=50")
            .then(res => res.json())
            .then(data => {
                const tbody = $("#table-verify-logs tbody");
                tbody.empty();
                if (data.success && data.logs && data.logs.length > 0) {
                    data.logs.forEach(l => {
                        const tr = $("<tr>");
                        tr.append($("<td>").text(l.created_at ? l.created_at.substring(0, 19).replace("T", " ") : "-"));
                        tr.append($("<td>").html(`<code>Team ${l.team_k}</code>`));
                        tr.append($("<td>").text(l.challenge_id));

                        const resBadge = l.passed
                            ? "<span class='badge badge-success'>PASSED</span>"
                            : "<span class='badge badge-danger'>FAILED</span>";
                        tr.append($("<td>").html(resBadge));
                        tr.append($("<td>").text(`${l.duration_ms} ms`));
                        tr.append($("<td>").text(l.message));

                        tbody.append(tr);
                    });
                } else {
                    tbody.html("<tr><td colspan='6' class='text-center text-muted'>No verification history recorded yet.</td></tr>");
                }
            })
            .catch(() => {});
    }

    // ==========================================
    // 5. Plugin Settings Save
    // ==========================================
    $("#form-plugin-settings").on("submit", function (e) {
        e.preventDefault();
        const payload = {
            blueagent_url: $("#setting-url").val(),
            blueagent_token: $("#setting-token").val(),
            verify_cooldown: parseInt($("#setting-cooldown").val())
        };

        const feedback = $("#settings-save-feedback");
        feedback.text("Saving...").css("color", "blue");

        safeFetchJson("/admin/plugins/blueagent/api/settings", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ ...payload, nonce: getNonce() })
        })
        .then(data => {
            if (data.success) {
                feedback.text("✓ Settings saved successfully!").css("color", "green");
                setTimeout(loadOverview, 1000);
            } else {
                feedback.text("✗ Failed to save settings: " + (data.message || "")).css("color", "red");
            }
        })
        .catch(err => {
            feedback.text("✗ Network error: " + err.message).css("color", "red");
        });
    });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
