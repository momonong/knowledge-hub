#!/usr/bin/env bash
set -euo pipefail

android_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
sdk_dir=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}
: "${sdk_dir:?Set ANDROID_HOME to the Android SDK}"
: "${JAVA_HOME:?Set JAVA_HOME to JDK 17}"
: "${KNOWLEDGE_HUB_SIGNING_DIR:?Set KNOWLEDGE_HUB_SIGNING_DIR to the private Knowledge Hub signing directory}"

tools_dir="$sdk_dir/build-tools/37.0.0"
keystore="$KNOWLEDGE_HUB_SIGNING_DIR/release.p12"
password_file="$KNOWLEDGE_HUB_SIGNING_DIR/release-password"
for required in "$keystore" "$password_file"; do
    if [[ ! -f "$required" || ! -r "$required" ]]; then
        printf 'Missing release signing file: %s\n' "$required" >&2
        exit 1
    fi
done

export PATH="$JAVA_HOME/bin:$PATH"
gradle=${KNOWLEDGE_HUB_GRADLE:-"$android_dir/gradlew"}
"$gradle" -p "$android_dir" assembleRelease "$@"

unsigned=$(python3 - "$android_dir/build/outputs/apk/release/output-metadata.json" <<'PY'
import json
import sys
from pathlib import Path

metadata = Path(sys.argv[1])
output, = json.loads(metadata.read_text())["elements"]
filename = Path(output["outputFile"])
apk = metadata.parent / filename
if filename != Path(filename.name) or not apk.is_file():
    raise SystemExit("Gradle output metadata does not name a release APK.")
print(apk)
PY
)
staging=$(mktemp -d "$android_dir/build/.sign.XXXXXXXX")
trap 'rm -rf "$staging"' EXIT
apk="$staging/knowledge-hub-android.apk"
aapt="$tools_dir/aapt2"

"$aapt" dump badging "$unsigned" > "$staging/badging.txt"
"$aapt" dump xmltree --file AndroidManifest.xml "$unsigned" > "$staging/manifest.txt"
"$aapt" dump resources "$unsigned" > "$staging/resources.txt"
icon=$(python3 - "$staging/badging.txt" "$staging/manifest.txt" "$staging/resources.txt" <<'PY'
import re
import sys
from pathlib import Path

badging, manifest, resources = [Path(path).read_text() for path in sys.argv[1:]]

def require(condition, reason):
    if not condition:
        raise SystemExit(f"Release APK rejected: {reason}")

require(
    re.search(r"^package: name='com\.momonong\.knowledgehub' versionCode='[1-9][0-9]*' versionName='[^']+'", badging, re.M),
    "package or version does not match Knowledge Hub",
)
require(
    re.search(r"^minSdkVersion:'26'$", badging, re.M)
    and re.search(r"^targetSdkVersion:'37'$", badging, re.M)
    and " compileSdkVersion='37'" in badging,
    "expected min SDK 26 and target/compile SDK 37",
)
require(
    "launchable-activity: name='com.momonong.knowledgehub.MainActivity'" in badging,
    "expected launcher activity is absent",
)
require(
    sorted(re.findall(r"^uses-permission[^:]*: name='([^']+)'", badging, re.M)) == [
        "android.permission.INTERNET",
        "android.permission.REQUEST_INSTALL_PACKAGES",
        "android.permission.USE_BIOMETRIC",
        "android.permission.USE_FINGERPRINT",
    ],
    "manifest permissions differ from the network, in-app update and passkey allowlist",
)
require(
    "application-debuggable" not in badging and not re.search(r":debuggable\(0x[\da-f]+\)=true", manifest),
    "the application is debuggable",
)
for attr in ("allowBackup", "fullBackupContent", "usesCleartextTraffic"):
    require(re.search(rf":{attr}\(0x[\da-f]+\)=false", manifest), f"{attr} must be false")
icon_id = re.search(r":icon\(0x[\da-f]+\)=@(0x[\da-f]+)", manifest)
require(
    icon_id and re.search(rf"^\s+resource {icon_id[1]} mipmap/ic_launcher$", resources, re.M),
    "expected the mipmap/ic_launcher reference",
)
icon = re.search(r"^application: .*? icon='(res/[^']+\.xml)'", badging, re.M)
require(icon, "expected the adaptive launcher icon XML")
print(icon[1])
PY
)
"$aapt" dump xmltree --file "$icon" "$unsigned" > "$staging/icon.txt"
if ! grep -Fq 'E: adaptive-icon ' "$staging/icon.txt"; then
    printf 'Release APK rejected: ic_launcher must use an adaptive icon.\n' >&2
    exit 1
fi

"$tools_dir/zipalign" -P 16 -f 4 "$unsigned" "$staging/aligned.apk"
"$tools_dir/apksigner" sign --ks "$keystore" --ks-key-alias knowledge-hub-release \
    --ks-pass "file:$password_file" --out "$apk" "$staging/aligned.apk"
"$tools_dir/apksigner" verify --verbose --print-certs "$apk"
"$tools_dir/zipalign" -c -P 16 4 "$apk"
"$aapt" dump badging "$apk" > "$staging/signed-badging.txt"
python3 - "$apk" "$staging/signed-badging.txt" "$staging/android-release.json" <<'PY_RELEASE'
import hashlib
import json
import re
import sys
from pathlib import Path

apk, badging, metadata = map(Path, sys.argv[1:])
identity = re.search(
    r"^package: name='(com\.momonong\.knowledgehub)' versionCode='([1-9][0-9]*)' versionName='([^']+)'",
    badging.read_text(),
    re.M,
)
if not identity:
    raise SystemExit("Signed APK does not contain the expected package and version information.")

digest = hashlib.sha256()
size = 0
with apk.open("rb") as source:
    while chunk := source.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
metadata.write_text(json.dumps({
    "applicationId": identity[1],
    "versionCode": int(identity[2]),
    "versionName": identity[3],
    "sizeBytes": size,
    "sha256": digest.hexdigest(),
}) + "\n", encoding="utf-8")
PY_RELEASE
mv "$apk" "$android_dir/build/knowledge-hub-android.apk"
mv "$staging/android-release.json" "$android_dir/build/android-release.json"
printf '%s\n' "$android_dir/build/knowledge-hub-android.apk"
