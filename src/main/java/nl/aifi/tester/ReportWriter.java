package nl.aifi.tester;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.function.Function;

/**
 * Writes the test report: {@code report.html} (for people), {@code results.csv} (for Excel)
 * and {@code results.json}. Only study references (hashes), never patient data.
 */
public final class ReportWriter {

    private static final DateTimeFormatter TS = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss").withZone(ZoneId.systemDefault());

    private final Path dir;
    private final TesterConfig cfg;
    private final long startedAt;
    private final int libraryStudies;

    public ReportWriter(Path dir, TesterConfig cfg, long startedAt, int libraryStudies) {
        this.dir = dir;
        this.cfg = cfg;
        this.startedAt = startedAt;
        this.libraryStudies = libraryStudies;
    }

    public Path dir() { return dir; }

    public synchronized void write(Tracker tracker, String state) throws IOException {
        Files.createDirectories(dir);
        List<TestRun> runs = tracker.runs();
        atomic(dir.resolve("results.csv"), csv(runs));
        atomic(dir.resolve("results.json"), json(runs, tracker, state));
        atomic(dir.resolve("report.html"), html(runs, tracker, state));
    }

    // ── statistics ───────────────────────────────────────────────────────────

    static final class Stats {
        int sent, pass, sendFailed, timeout, pending, notFinished;
        final List<Double> ttfr = new ArrayList<>();
        final List<Double> send = new ArrayList<>();

        void add(TestRun r) {
            sent++;
            switch (r.outcome) {
                case PASS: pass++; break;
                case SEND_FAILED: sendFailed++; break;
                case TIMEOUT: timeout++; break;
                case NOT_FINISHED: notFinished++; break;
                default: pending++;
            }
            if (r.secondsToFirstResult() >= 0) ttfr.add(r.secondsToFirstResult());
            if (r.sendSeconds() >= 0 && r.outcome != TestRun.Outcome.SEND_FAILED) send.add(r.sendSeconds());
        }

        int decided() { return pass + sendFailed + timeout; }

        String passRate() {
            return decided() == 0 ? "-" : String.format(Locale.ROOT, "%.1f %%", 100.0 * pass / decided());
        }
    }

    static double percentile(List<Double> values, double p) {
        if (values.isEmpty()) return -1;
        List<Double> v = new ArrayList<>(values);
        v.sort(Double::compare);
        int i = (int) Math.ceil(p / 100.0 * v.size()) - 1;
        return v.get(Math.max(0, Math.min(v.size() - 1, i)));
    }

    private static <K> Map<K, Stats> group(List<TestRun> runs, Function<TestRun, K> key) {
        Map<K, Stats> out = new LinkedHashMap<>();
        for (TestRun r : runs) out.computeIfAbsent(key.apply(r), k -> new Stats()).add(r);
        return out;
    }

    // ── CSV / JSON ───────────────────────────────────────────────────────────

    private static String csv(List<TestRun> runs) {
        StringBuilder sb = new StringBuilder("id;schedule;target;study_ref;modality;instances;bytes;sent_at;send_s;"
                + "instances_accepted;send_error;first_result_s;result_series;outcome\n");
        for (TestRun r : runs) {
            sb.append(r.id).append(';').append(r.schedule).append(';').append(r.target).append(';').append(r.studyRef).append(';')
              .append(r.modality).append(';').append(r.instances).append(';').append(r.bytes).append(';')
              .append(r.sendStartedAt == 0 ? "" : TS.format(Instant.ofEpochMilli(r.sendStartedAt))).append(';')
              .append(num(r.sendSeconds())).append(';').append(r.instancesAccepted).append(';')
              .append(csvText(r.sendError)).append(';').append(num(r.secondsToFirstResult())).append(';')
              .append(csvText(r.resultSummary())).append(';').append(r.outcome).append('\n');
        }
        return sb.toString();
    }

    private static String csvText(String s) {
        return s == null ? "" : "\"" + s.replace("\"", "\"\"").replace('\n', ' ') + "\"";
    }

    private static String num(double v) {
        return v < 0 ? "" : String.format(Locale.ROOT, "%.1f", v);
    }

    private String json(List<TestRun> runs, Tracker tracker, String state) {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("state", state);
        root.put("startedAt", TS.format(Instant.ofEpochMilli(startedAt)));
        root.put("updatedAt", TS.format(Instant.now()));
        Stats all = new Stats();
        runs.forEach(all::add);
        Map<String, Object> sum = new LinkedHashMap<>();
        sum.put("sent", all.sent);
        sum.put("pass", all.pass);
        sum.put("sendFailed", all.sendFailed);
        sum.put("timeout", all.timeout);
        sum.put("pending", all.pending);
        sum.put("notFinished", all.notFinished);
        sum.put("medianSecondsToFirstResult", percentile(all.ttfr, 50));
        sum.put("p90SecondsToFirstResult", percentile(all.ttfr, 90));
        sum.put("unmatchedObjects", tracker.unmatched().size());
        sum.put("unreadableObjects", tracker.unreadable().size());
        root.put("summary", sum);
        List<Object> list = new ArrayList<>();
        for (TestRun r : runs) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", r.id);
            m.put("schedule", r.schedule);
            m.put("target", r.target);
            m.put("studyRef", r.studyRef);
            m.put("instances", r.instances);
            m.put("sentAt", r.sendStartedAt == 0 ? "" : TS.format(Instant.ofEpochMilli(r.sendStartedAt)));
            m.put("sendSeconds", r.sendSeconds());
            m.put("instancesAccepted", r.instancesAccepted);
            m.put("sendError", r.sendError);
            m.put("secondsToFirstResult", r.secondsToFirstResult());
            m.put("results", r.resultSummary());
            m.put("outcome", r.outcome.name());
            list.add(m);
        }
        root.put("tests", list);
        return Json.write(root);
    }

    // ── HTML ─────────────────────────────────────────────────────────────────

    private String html(List<TestRun> runs, Tracker tracker, String state) {
        Stats all = new Stats();
        runs.forEach(all::add);
        StringBuilder h = new StringBuilder(16_384);
        h.append("<!doctype html><html lang=\"nl\"><head><meta charset=\"utf-8\">")
         .append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
         .append("<title>AIFI testrapport</title>");
        if (!"afgerond".equals(state)) h.append("<meta http-equiv=\"refresh\" content=\"").append(cfg.report.refreshSeconds).append("\">");
        h.append("<style>").append(CSS).append("</style></head><body><main>");
        h.append("<h1>AIFI Gateway testrapport</h1>");
        h.append("<p class=\"meta\">Gestart ").append(TS.format(Instant.ofEpochMilli(startedAt)))
         .append(" &middot; bijgewerkt ").append(TS.format(Instant.now()))
         .append(" &middot; status <b>").append(esc(state)).append("</b> &middot; ")
         .append(libraryStudies).append(" teststudies in de map</p>");

        h.append("<section class=\"tiles\">");
        tile(h, "Verstuurd", Integer.toString(all.sent), "");
        tile(h, "Geslaagd", Integer.toString(all.pass), "ok");
        tile(h, "Geen resultaat", Integer.toString(all.timeout), all.timeout > 0 ? "bad" : "");
        tile(h, "Verzenden mislukt", Integer.toString(all.sendFailed), all.sendFailed > 0 ? "bad" : "");
        tile(h, "Bezig", Integer.toString(all.pending), "");
        tile(h, "Slagingspercentage", all.passRate(), all.decided() > 0 && all.pass < all.decided() ? "bad" : "ok");
        tile(h, "Mediaan tot resultaat", secs(percentile(all.ttfr, 50)), "");
        tile(h, "P90 tot resultaat", secs(percentile(all.ttfr, 90)), "");
        h.append("</section>");

        h.append("<h2>Tijdlijn</h2><p class=\"hint\">Per test: verzendmoment (horizontaal) en tijd tot het eerste AI-resultaat (verticaal). "
                + "Rood bovenaan = geen resultaat binnen de time-out, grijs onderaan = verzenden mislukt.</p>");
        timeline(h, runs);

        h.append("<h2>Per poort</h2>");
        statsTable(h, "Poort", group(runs, r -> r.target));
        h.append("<h2>Per schema</h2>");
        statsTable(h, "Schema", group(runs, r -> r.schedule));

        h.append("<h2>Alle tests</h2><div class=\"scroll\"><table><thead><tr><th>Test</th><th>Schema</th><th>Poort</th>"
                + "<th>Studie</th><th class=\"n\">Beelden</th><th>Verstuurd</th><th class=\"n\">Verzenden</th>"
                + "<th class=\"n\">Tot resultaat</th><th>Resultaat</th><th>Uitkomst</th></tr></thead><tbody>");
        for (int i = runs.size() - 1; i >= 0; i--) {
            TestRun r = runs.get(i);
            h.append("<tr><td>").append(r.id).append("</td><td>").append(esc(r.schedule)).append("</td><td>").append(esc(r.target))
             .append("</td><td><code>").append(r.studyRef).append("</code> ").append(esc(r.modality)).append("</td><td class=\"n\">")
             .append(r.instancesAccepted).append('/').append(r.instances).append("</td><td>")
             .append(r.sendStartedAt == 0 ? "-" : TS.format(Instant.ofEpochMilli(r.sendStartedAt))).append("</td><td class=\"n\">")
             .append(secs(r.sendSeconds())).append("</td><td class=\"n\">").append(secs(r.secondsToFirstResult())).append("</td><td>")
             .append(esc(r.resultSummary().isEmpty() ? r.sendError : r.resultSummary())).append("</td><td><span class=\"b ")
             .append(r.outcome.name().toLowerCase(Locale.ROOT)).append("\">").append(label(r.outcome)).append("</span></td></tr>");
        }
        h.append("</tbody></table></div>");

        List<Tracker.Unmatched> um = tracker.unmatched();
        h.append("<h2>Ontvangen zonder bijbehorende test</h2><p>").append(um.size())
         .append(" object(en) ontvangen die bij geen test horen; ").append(tracker.echoes())
         .append(" terugontvangen beeld(en) uit de verzonden studies zelf (niet meegeteld als resultaat).</p>");
        if (!um.isEmpty()) {
            h.append("<div class=\"scroll\"><table><thead><tr><th>Ontvangen</th><th>Van AE</th><th>Modaliteit</th><th>Serie</th></tr></thead><tbody>");
            for (int i = um.size() - 1; i >= Math.max(0, um.size() - 50); i--) {
                Tracker.Unmatched u = um.get(i);
                h.append("<tr><td>").append(TS.format(Instant.ofEpochMilli(u.at))).append("</td><td>").append(esc(u.callingAe))
                 .append("</td><td>").append(esc(u.modality)).append("</td><td>").append(esc(u.description)).append("</td></tr>");
            }
            h.append("</tbody></table></div>");
        }

        List<String[]> bad = tracker.unreadable();
        if (!bad.isEmpty()) {
            h.append("<h2>Onleesbare objecten</h2><p>").append(bad.size()).append(" object(en) ontvangen die geen gewone DICOM-dataset waren. ")
             .append("Wat niet te lezen was, staat in de map <code>unreadable</code> van deze sessie. Een ZIP met DICOM erin pakt de tester uit en telt hij mee ")
             .append("(zie &quot;via ZIP&quot; bij de test), maar een PACS weigert dit: de verzender moet gewone DICOM sturen.</p>")
             .append("<div class=\"scroll\"><table><thead><tr><th>Ontvangen</th><th>Van AE</th><th>SOP-klasse</th><th>Inhoud</th>")
             .append("<th>Afhandeling</th></tr></thead><tbody>");
            for (int i = bad.size() - 1; i >= Math.max(0, bad.size() - 50); i--) {
                String[] u = bad.get(i);
                h.append("<tr><td>").append(TS.format(Instant.ofEpochMilli(Long.parseLong(u[0])))).append("</td><td>").append(esc(u[1]))
                 .append("</td><td>").append(esc(u[2])).append("</td><td>").append(esc(u[3])).append("</td><td>").append(esc(u[4]))
                 .append("</td></tr>");
            }
            h.append("</tbody></table></div>");
        }

        h.append("<h2>Instellingen</h2><ul>");
        for (TesterConfig.Target t : cfg.targets) {
            h.append("<li>Poort <b>").append(esc(t.name)).append("</b>: ").append(esc(t.aeTitle)).append('@').append(esc(t.host))
             .append(':').append(t.port).append(t.tls.enabled ? " (TLS)" : "").append(", gewicht ").append(t.weight).append("</li>");
        }
        for (TesterConfig.Schedule s : cfg.schedules) {
            h.append("<li>Schema <b>").append(esc(s.name)).append("</b>: ").append(s.count).append(" studie(s) elke ")
             .append(s.everyMinutes).append(" min").append(s.target.isEmpty() ? "" : " naar " + esc(s.target)).append("</li>");
        }
        h.append("<li>Resultaat terug op ").append(esc(cfg.receiver.aeTitle)).append(':').append(cfg.receiver.port)
         .append(", time-out ").append(cfg.matching.resultTimeoutMinutes).append(" min, unieke UID's per test: ")
         .append(cfg.studies.uniquify ? "ja" : "nee").append("</li></ul>");
        h.append("</main></body></html>");
        return h.toString();
    }

    private static void tile(StringBuilder h, String label, String value, String cls) {
        h.append("<div class=\"tile ").append(cls).append("\"><div class=\"v\">").append(esc(value))
         .append("</div><div class=\"l\">").append(esc(label)).append("</div></div>");
    }

    private static void statsTable(StringBuilder h, String what, Map<String, Stats> groups) {
        h.append("<div class=\"scroll\"><table><thead><tr><th>").append(what).append("</th><th class=\"n\">Verstuurd</th>"
                + "<th class=\"n\">Geslaagd</th><th class=\"n\">Geen resultaat</th><th class=\"n\">Verzenden mislukt</th>"
                + "<th class=\"n\">Bezig</th><th class=\"n\">Slagings&shy;percentage</th><th class=\"n\">Verzenden mediaan</th>"
                + "<th class=\"n\">Tot resultaat mediaan</th><th class=\"n\">P90</th><th class=\"n\">Max</th></tr></thead><tbody>");
        for (Map.Entry<String, Stats> e : groups.entrySet()) {
            Stats s = e.getValue();
            h.append("<tr><td>").append(esc(e.getKey())).append("</td><td class=\"n\">").append(s.sent).append("</td><td class=\"n\">")
             .append(s.pass).append("</td><td class=\"n\">").append(s.timeout).append("</td><td class=\"n\">").append(s.sendFailed)
             .append("</td><td class=\"n\">").append(s.pending).append("</td><td class=\"n\">").append(s.passRate())
             .append("</td><td class=\"n\">").append(secs(percentile(s.send, 50))).append("</td><td class=\"n\">")
             .append(secs(percentile(s.ttfr, 50))).append("</td><td class=\"n\">").append(secs(percentile(s.ttfr, 90)))
             .append("</td><td class=\"n\">").append(secs(percentile(s.ttfr, 100))).append("</td></tr>");
        }
        h.append("</tbody></table></div>");
    }

    private void timeline(StringBuilder h, List<TestRun> runs) {
        int w = 960, ht = 260, left = 56, bottom = 28, top = 14;
        long t0 = startedAt;
        long t1 = Math.max(System.currentTimeMillis(), t0 + 60_000);
        double maxY = 60;
        for (TestRun r : runs) maxY = Math.max(maxY, r.secondsToFirstResult());
        maxY = Math.max(maxY, cfg.matching.resultTimeoutMinutes * 60.0 * 0.25);
        maxY = Math.ceil(maxY / 60) * 60;
        h.append("<div class=\"scroll\"><svg class=\"chart\" viewBox=\"0 0 ").append(w).append(' ').append(ht)
         .append("\" role=\"img\" aria-label=\"Tijd tot AI-resultaat per test\">");
        for (int i = 0; i <= 4; i++) {
            double v = maxY * i / 4;
            double y = ht - bottom - (ht - bottom - top) * i / 4.0;
            h.append("<line class=\"grid\" x1=\"").append(left).append("\" x2=\"").append(w - 8).append("\" y1=\"").append(f(y))
             .append("\" y2=\"").append(f(y)).append("\"/><text class=\"ax\" x=\"").append(left - 6).append("\" y=\"").append(f(y + 4))
             .append("\" text-anchor=\"end\">").append(secs(v)).append("</text>");
        }
        h.append("<text class=\"ax\" x=\"").append(left).append("\" y=\"").append(ht - 8).append("\">")
         .append(TS.format(Instant.ofEpochMilli(t0))).append("</text><text class=\"ax\" x=\"").append(w - 8).append("\" y=\"")
         .append(ht - 8).append("\" text-anchor=\"end\">").append(TS.format(Instant.ofEpochMilli(t1))).append("</text>");
        for (TestRun r : runs) {
            if (r.sendStartedAt == 0) continue;
            double x = left + (w - 8 - left) * (double) (r.sendStartedAt - t0) / (t1 - t0);
            String cls;
            double y;
            if (r.outcome == TestRun.Outcome.SEND_FAILED) { cls = "fail"; y = ht - bottom; }
            else if (r.secondsToFirstResult() >= 0) { cls = "pass"; y = ht - bottom - (ht - bottom - top) * Math.min(1, r.secondsToFirstResult() / maxY); }
            else if (r.outcome == TestRun.Outcome.TIMEOUT) { cls = "timeout"; y = top; }
            else continue;
            h.append("<circle class=\"").append(cls).append("\" cx=\"").append(f(x)).append("\" cy=\"").append(f(y))
             .append("\" r=\"4\"><title>").append(r.id).append(' ').append(esc(r.target)).append(": ").append(label(r.outcome))
             .append(r.secondsToFirstResult() >= 0 ? ", " + secs(r.secondsToFirstResult()) : "").append("</title></circle>");
        }
        h.append("</svg></div>");
    }

    private static String label(TestRun.Outcome o) {
        switch (o) {
            case PASS: return "geslaagd";
            case SEND_FAILED: return "verzenden mislukt";
            case TIMEOUT: return "geen resultaat";
            case NOT_FINISHED: return "niet afgewacht";
            default: return "bezig";
        }
    }

    private static String secs(double s) {
        if (s < 0) return "-";
        if (s < 120) return String.format(Locale.ROOT, "%.1f s", s);
        return String.format(Locale.ROOT, "%d:%02d min", (long) s / 60, (long) s % 60);
    }

    private static String f(double v) { return String.format(Locale.ROOT, "%.1f", v); }

    static String esc(String s) {
        if (s == null) return "";
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;");
    }

    private static void atomic(Path file, String text) throws IOException {
        Path tmp = file.resolveSibling(file.getFileName() + ".tmp");
        Files.writeString(tmp, text, StandardCharsets.UTF_8);
        Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
    }

    private static final String CSS = ":root{--bg:#f7f8fa;--card:#fff;--fg:#1d2330;--mut:#5b6474;--line:#e3e6eb;--ok:#1a7f4b;--bad:#c0392b;--acc:#2f6fdb}"
            + "@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e8eaee;--mut:#9aa3b2;--line:#2c313a;--ok:#3fbf7f;--bad:#ff6b5a;--acc:#6f9ef5}}"
            + "body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}"
            + "main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:22px;margin:8px 0}h2{font-size:17px;margin:28px 0 8px}"
            + ".meta,.hint{color:var(--mut)}.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}"
            + ".tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}"
            + ".tile .l{color:var(--mut);font-size:12px}.tile.ok .v{color:var(--ok)}.tile.bad .v{color:var(--bad)}"
            + ".scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}"
            + "table{border-collapse:collapse;width:100%}th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}"
            + "th{font-size:12px;color:var(--mut);font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}"
            + ".b{padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid var(--line)}.b.pass{color:var(--ok);border-color:var(--ok)}"
            + ".b.timeout,.b.send_failed{color:var(--bad);border-color:var(--bad)}code{font-size:12px}"
            + ".chart{width:100%;min-width:640px;display:block}.chart .grid{stroke:var(--line)}.chart .ax{fill:var(--mut);font-size:11px}"
            + ".chart .pass{fill:var(--acc)}.chart .timeout{fill:var(--bad)}.chart .fail{fill:var(--mut)}";
}
