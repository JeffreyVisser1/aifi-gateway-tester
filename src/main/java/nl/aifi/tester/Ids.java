package nl.aifi.tester;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.UUID;

/** Identifier helpers. The report never shows original identifiers, only short hashes. */
final class Ids {

    private Ids() {}

    /** Short, stable, non-reversible reference for an original identifier. */
    static String ref(String value) {
        try {
            byte[] h = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 4; i++) sb.append(String.format("%02x", h[i]));
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** A new UID under the UUID-derived root 2.25 (PS3.5 §B.2). */
    static String newUid() {
        UUID u = UUID.randomUUID();
        return "2.25." + new java.math.BigInteger(1, java.nio.ByteBuffer.allocate(16)
                .putLong(u.getMostSignificantBits()).putLong(u.getLeastSignificantBits()).array());
    }
}
