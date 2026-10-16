// BlueAgent Create Challenge Controller
CTFd.plugin.run((_CTFd) => {
    const $ = (_CTFd && _CTFd.lib && _CTFd.lib.$) || window.$;

    // Toggle fallback flag field
    $("#allow_flag_fallback").on("change", function () {
        if ($(this).is(":checked")) {
            $("#static_flag_group").slideDown();
        } else {
            $("#static_flag_group").slideUp();
        }
    });

    // Auto-read spec file into textarea when selected
    $("#spec_file").on("change", function (e) {
        const file = e.target.files[0];
        if (file) {
            const reader = new FileReader();
            reader.onload = function (evt) {
                $("#spec_text").val(evt.target.result);
                validateSpec();
            };
            reader.readAsText(file);
        }
    });

    // Validate button handler
    $("#btn-validate-spec").on("click", function () {
        validateSpec();
    });

    function validateSpec() {
        const specText = $("#spec_text").val();
        if (!specText.trim()) {
            $("#spec-validation-feedback").text("Please paste or upload specification content.").css("color", "red");
            return;
        }

        $("#spec-validation-feedback").text("Validating...").css("color", "blue");

        fetch("/admin/plugins/blueagent/api/spec/validate", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "CSRF-Token": window.init.csrfNonce
            },
            body: JSON.stringify({ spec_text: specText })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                $("#spec-validation-feedback").text("✓ Specification Valid (" + data.format.toUpperCase() + ")").css("color", "green");

                // Populate preview table
                const tbody = $("#preview-containers-table tbody");
                tbody.empty();
                data.containers.forEach(c => {
                    const tr = $("<tr>");
                    tr.append($("<td>").text(c.name));
                    tr.append($("<td>").text(c.image));
                    tr.append($("<td>").html("<code>10.{{team_k}}.1." + c.ip_suffix + "</code>"));
                    tr.append($("<td>").text(c.limits_memory || "256MB"));
                    tr.append($("<td>").text(c.limits_cpu || "1"));
                    tbody.append(tr);
                });

                $("#preview-probes-summary").html(
                    "<strong>Verifier Image:</strong> <code>" + (data.verifier.image || "default") + "</code> | " +
                    "<strong>Readiness Checks:</strong> " + data.readiness_count + " | " +
                    "<strong>Verify Checks:</strong> " + data.verify_count
                );
            } else {
                $("#spec-validation-feedback").text("✗ " + data.message).css("color", "red");
            }
        })
        .catch(err => {
            $("#spec-validation-feedback").text("Network error validating spec: " + err).css("color", "red");
        });
    }
});
