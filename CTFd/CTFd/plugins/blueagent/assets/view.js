// BlueAgent Player Challenge Controller
(function () {
    const $ = window.$ || (window.CTFd && window.CTFd.lib && window.CTFd.lib.$);
    let vpnPollInterval = null;
    let timerInterval = null;
    let countdownInterval = null;

    // CTFd challenge view lifecycle contract
    CTFd._internal.challenge.data = undefined;
    CTFd._internal.challenge.renderer = null;
    CTFd._internal.challenge.preRender = function () {};
    CTFd._internal.challenge.render = null;

    CTFd._internal.challenge.postRender = function () {
        const challengeId = (CTFd._internal.challenge.data && CTFd._internal.challenge.data.id)
            || parseInt($("#challenge-id").val())
            || (window.Alpine && window.Alpine.store("challenge") && window.Alpine.store("challenge").data && window.Alpine.store("challenge").data.id);
        if (!challengeId) return;

        loadPlayerStatus(challengeId);
        startVpnPolling();

        // Clean up intervals on modal close
        $(".modal").on("hidden.bs.modal", function () {
            clearInterval(vpnPollInterval);
            clearInterval(timerInterval);
            clearInterval(countdownInterval);
        });
    };

    CTFd._internal.challenge.submit = function (preview) {
        const challengeId = (CTFd._internal.challenge.data && CTFd._internal.challenge.data.id)
            || parseInt(CTFd.lib.$("#challenge-id").val());
        const submission = CTFd.lib.$("#challenge-input").val() || "VERIFIED_PATCH";

        const body = {
            challenge_id: challengeId,
            submission: submission
        };
        const params = {};
        if (preview) {
            params["preview"] = true;
        }

        if (CTFd.api && CTFd.api.post_challenge_attempt) {
            return CTFd.api.post_challenge_attempt(params, body).then(function (response) {
                if (response.status === 429 || response.status === 403) {
                    return response;
                }
                return response;
            });
        }

        return CTFd.fetch("/api/v1/challenges/attempt" + (preview ? "?preview=true" : ""), {
            method: "POST",
            body: JSON.stringify(body)
        }).then(function (res) { return res.json(); });
    };

    // Event delegation with namespace to prevent duplicate binds
    $(document).off("click.blueagent", "#btn-verify-patch").on("click.blueagent", "#btn-verify-patch", function () {
        const challengeId = (CTFd._internal.challenge.data && CTFd._internal.challenge.data.id)
            || parseInt($("#challenge-id").val())
            || (window.Alpine && window.Alpine.store("challenge") && window.Alpine.store("challenge").data && window.Alpine.store("challenge").data.id);
        if (challengeId) {
            triggerVerification(challengeId);
        }
    });

    $(document).off("click.blueagent", "#btn-retry-verify").on("click.blueagent", "#btn-retry-verify", function () {
        $("#verify-state-failed").hide();
        $("#verify-state-idle").show();
        $("#blueagent-verify-container").removeClass("failed").addClass("idle");
    });

    $(document).off("click.blueagent", "#btn-start-env").on("click.blueagent", "#btn-start-env", function () {
        const challengeId = (CTFd._internal.challenge.data && CTFd._internal.challenge.data.id)
            || parseInt($("#challenge-id").val())
            || (window.Alpine && window.Alpine.store("challenge") && window.Alpine.store("challenge").data && window.Alpine.store("challenge").data.id);
        if (!challengeId) return;

        const $btn = $(this);
        $btn.prop("disabled", true).text("Starting...");

        const csrf = (window.init && window.init.csrfNonce) || (window.CTFd && window.CTFd.config && window.CTFd.config.csrfNonce) || "";
        fetch(`/api/v1/plugins/blueagent/challenges/${challengeId}/start`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "CSRF-Token": csrf
            }
        })
        .then(res => res.json())
        .then(data => {
            $btn.prop("disabled", false).text("▶ Start Environment");
            if (data.success) {
                loadPlayerStatus(challengeId);
            } else {
                alert("Failed to start environment: " + (data.message || "Unknown error"));
            }
        })
        .catch(err => {
            $btn.prop("disabled", false).text("▶ Start Environment");
            alert("Network error starting environment: " + err);
        });
    });

    // Delegate click on Teardown Challenge
    $(document).off("click.blueagent", "#btn-teardown-env").on("click.blueagent", "#btn-teardown-env", function () {
        const challengeId = parseInt($("#challenge-id").val())
            || (window.CTFd && window.CTFd._internal && window.CTFd._internal.challenge && window.CTFd._internal.challenge.data && window.CTFd._internal.challenge.data.id)
            || (window.Alpine && window.Alpine.store("challenge") && window.Alpine.store("challenge").data && window.Alpine.store("challenge").data.id);
        if (!challengeId) return;

        const $btn = $(this);
        $btn.prop("disabled", true).text("Tearing down...");

        const csrf = (window.init && window.init.csrfNonce) || (window.CTFd && window.CTFd.config && window.CTFd.config.csrfNonce) || "";
        fetch(`/api/v1/plugins/blueagent/challenges/${challengeId}/teardown`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "CSRF-Token": csrf
            }
        })
        .then(res => res.json())
        .then(data => {
            $btn.prop("disabled", false).text("⏹ Teardown Challenge");
            if (data.success) {
                loadPlayerStatus(challengeId);
            } else {
                alert("Failed to teardown challenge: " + (data.message || "Unknown error"));
            }
        })
        .catch(err => {
            $btn.prop("disabled", false).text("⏹ Teardown Challenge");
            alert("Network error tearing down challenge: " + err);
        });
    });

    function loadPlayerStatus(challengeId) {
        fetch(`/api/v1/plugins/blueagent/status?challenge_id=${challengeId}`)
            .then(res => res.json())
            .then(data => {
                if (!data.success) return;

                $("#player-subnet").text(data.subnet || "10.?.1.0/24");

                // Target containers table
                const tbody = $("#player-targets-table tbody");
                tbody.empty();
                if (data.targets && data.targets.length > 0) {
                    data.targets.forEach(t => {
                        const tr = $("<tr>");
                        tr.append($("<td>").addClass("font-weight-bold").text(t.name));
                        tr.append($("<td>").html(`<code>${t.ip}</code>`));
                        tr.append($("<td>").text(t.limits_memory || "256MB"));
                        tr.append($("<td>").text(t.limits_cpu || "1"));
                        tbody.append(tr);
                    });
                } else {
                    tbody.append($("<tr>").append($("<td colspan='4'>").addClass("text-center text-muted").text("No containers registered.")));
                }

                // Check deployment status
                $("#deploy-controls").show();
                $("#deploy-status-label").text(`Status: ${data.deployment_status}`);
                if (data.deployment_status === "running") {
                    $("#btn-start-env").hide();
                    $("#btn-teardown-env").show();
                } else {
                    $("#btn-teardown-env").hide();
                    $("#btn-start-env").show();
                }

                // If already solved
                if (data.solved) {
                    showVerifyPassed("Challenge already marked solved by your team!");
                }
            })
            .catch(err => console.error("Error loading BlueAgent status:", err));
    }

    function startVpnPolling() {
        clearInterval(vpnPollInterval);
        function poll() {
            fetch("/api/v1/plugins/blueagent/vpn/status")
                .then(res => res.json())
                .then(data => {
                    const badge = $("#vpn-status-badge");
                    if (data.connected) {
                        badge.removeClass("badge-blueagent-offline").addClass("badge-blueagent-online").text("VPN Connected");
                    } else {
                        badge.removeClass("badge-blueagent-online").addClass("badge-blueagent-offline").text("VPN Disconnected");
                    }
                })
                .catch(() => {});
        }
        poll();
        vpnPollInterval = setInterval(poll, 15000);
    }

    function triggerVerification(challengeId) {
        // Show testing state
        $("#verify-state-idle").hide();
        $("#verify-state-failed").hide();
        $("#verify-state-busy").hide();
        $("#verify-state-testing").show();
        $("#blueagent-verify-container").removeClass("passed failed busy").addClass("testing");

        let elapsed = 0;
        clearInterval(timerInterval);
        $("#verify-elapsed-timer").text("Elapsed: 0s");
        timerInterval = setInterval(() => {
            elapsed++;
            $("#verify-elapsed-timer").text(`Elapsed: ${elapsed}s`);
        }, 1000);

        const csrf = (window.init && window.init.csrfNonce) || (window.CTFd && window.CTFd.config && window.CTFd.config.csrfNonce) || "";
        fetch(`/api/v1/plugins/blueagent/challenges/${challengeId}/verify`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "CSRF-Token": csrf
            }
        })
        .then(async (res) => {
            clearInterval(timerInterval);
            const data = await res.json().catch(() => ({}));
            const statusCode = res.status;

            if (statusCode === 200 && data.status === "passed") {
                showVerifyPassed(data.message);
            } else if (statusCode === 200 && data.status === "already_solved") {
                showVerifyPassed(data.message);
            } else if (statusCode === 200 && data.status === "failed") {
                showVerifyFailed(data.message, data.details);
            } else if (statusCode === 409 || statusCode === 429) {
                const waitSec = data.retry_after || 30;
                showVerifyBusy(data.message, waitSec);
            } else {
                showVerifyFailed(data.message || `Verification error (HTTP ${statusCode})`, null);
            }
        })
        .catch(err => {
            clearInterval(timerInterval);
            showVerifyFailed("Network error during verification: " + err, null);
        });
    }

    function showVerifyPassed(msg) {
        $("#verify-state-testing").hide();
        $("#verify-state-failed").hide();
        $("#verify-state-busy").hide();
        $("#verify-state-idle").hide();
        $("#verify-state-passed").show();
        $("#verify-passed-message").text(msg || "All tests passed successfully!");
        $("#blueagent-verify-container").removeClass("testing failed busy").addClass("passed");
    }

    function showVerifyFailed(msg, details) {
        $("#verify-state-testing").hide();
        $("#verify-state-passed").hide();
        $("#verify-state-busy").hide();
        $("#verify-state-idle").hide();
        $("#verify-state-failed").show();
        $("#verify-failed-message").text(msg || "Verification checks failed.");
        $("#blueagent-verify-container").removeClass("testing passed busy").addClass("failed");

        if (details && Object.keys(details).length > 0) {
            $("#verify-failed-details-content").text(JSON.stringify(details, null, 2));
            $("#verify-failed-details").show();
        } else {
            $("#verify-failed-details").hide();
        }
    }

    function showVerifyBusy(msg, seconds) {
        $("#verify-state-testing").hide();
        $("#verify-state-passed").hide();
        $("#verify-state-failed").hide();
        $("#verify-state-idle").hide();
        $("#verify-state-busy").show();
        $("#verify-busy-message").text(msg || "Verification currently busy. Please wait.");
        $("#blueagent-verify-container").removeClass("testing passed failed").addClass("busy");

        let remaining = seconds;
        $("#verify-countdown-seconds").text(remaining);
        clearInterval(countdownInterval);

        countdownInterval = setInterval(() => {
            remaining--;
            if (remaining <= 0) {
                clearInterval(countdownInterval);
                $("#verify-state-busy").hide();
                $("#verify-state-idle").show();
                $("#blueagent-verify-container").removeClass("busy").addClass("idle");
            } else {
                $("#verify-countdown-seconds").text(remaining);
            }
        }, 1000);
    }
})();
