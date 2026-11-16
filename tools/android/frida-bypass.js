/*
 * Fire TV Android app certificate-pinning bypass.
 *
 * The official Android app embeds Amazon's "Turnstile Server" certificate and
 * builds its OkHttp client with a TrustManager that trusts only that cert.
 * The private key is not in the APK, so no self-signed Linux certificate can
 * satisfy it. This script replaces the app's TrustManager with one that trusts
 * the receiver's certificate.
 *
 * Usage (rooted Android or Waydroid with frida-server):
 *   frida -U -f com.amazon.storm.lightning.client.aosp \
 *         -l frida-bypass.js --no-pause
 *
 * This affects only the instrumented process. It does not patch the APK.
 */

Java.perform(function () {
    var LOG = "[firetv-bypass] ";

    // 1. Trust-all X509TrustManager.
    var X509TrustManager = Java.use("javax.net.ssl.X509TrustManager");
    var TrustAll = Java.registerClass({
        name: "local.firetvreceiver.TrustAll",
        implements: [X509TrustManager],
        methods: {
            checkClientTrusted: function (chain, authType) {},
            checkServerTrusted: function (chain, authType) {},
            getAcceptedIssuers: function () { return []; }
        }
    });
    var trustAll = TrustAll.$new();

    // 2. Replace every SSLContext.init() TrustManager array.
    try {
        var SSLContext = Java.use("javax.net.ssl.SSLContext");
        var init = SSLContext.init.overload(
            "[Ljavax.net.ssl.KeyManager;",
            "[Ljavax.net.ssl.TrustManager;",
            "java.security.SecureRandom"
        );
        init.implementation = function (keyManagers, trustManagers, secureRandom) {
            console.log(LOG + "SSLContext.init -> trust-all");
            init.call(this, keyManagers, [trustAll], secureRandom);
        };
    } catch (e) {
        console.log(LOG + "SSLContext hook failed: " + e);
    }

    // 3. OkHttp certificate pinner, if the app uses one in addition to the
    //    custom TrustManager.
    try {
        var CertificatePinner = Java.use("okhttp3.CertificatePinner");
        CertificatePinner.check.overload("java.lang.String", "java.util.List")
            .implementation = function (host, peerCertificates) {
                console.log(LOG + "CertificatePinner.check(" + host + ") bypassed");
            };
        CertificatePinner.check.overload("java.lang.String", "[Ljava.security.cert.Certificate;")
            .implementation = function (host, peerCertificates) {
                console.log(LOG + "CertificatePinner.check[](" + host + ") bypassed");
            };
    } catch (e) {
        // Not all app versions use OkHttp's pinner.
    }

    // 4. Hostname verification is already disabled in the tested 4.9.1 build,
    //    but return true if a verifier is present.
    try {
        var OkHostnameVerifier = Java.use("okhttp3.internal.tls.OkHostnameVerifier");
        OkHostnameVerifier.verify.overload(
            "java.lang.String", "javax.net.ssl.SSLSession"
        ).implementation = function (host, session) {
            return true;
        };
    } catch (e) {
        // Class may be obfuscated or absent.
    }

    try {
        var HostnameVerifier = Java.use("javax.net.ssl.HostnameVerifier");
        // Interface only; the app's own verifier already returns true.
        void HostnameVerifier;
    } catch (e) {
        // ignore
    }

    console.log(LOG + "loaded");
});
