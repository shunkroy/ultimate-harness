/* harness-ffi device proof — a plain C caller.
 *
 * Build (Termux/PRoot, aarch64):
 *   cd /opt/harness2/native
 *   gcc -o /tmp/ffi_proof ../device/ffi_proof/main.c \
 *       -L target/debug -lharness_ffi \
 *       -Wl,-rpath,/opt/harness2/native/target/debug
 *   /tmp/ffi_proof
 *
 * Expected: every CHECK line passes, final line "FFI PROOF: PASS".
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

extern unsigned int harness_abi_version(void);
extern char *harness_version(void);
extern char *harness_last_error(void);
extern void harness_string_free(char *ptr);
extern int harness_world_compile(const char *source_path, const char *world_id,
                                 const char *title, const char *out_dir);
extern int harness_world_open(const char *package_dir, const char *state_root,
                              const char *instance_id, const char *branch_id,
                              unsigned long long *handle_out);
extern int harness_world_act(unsigned long long handle, const char *utterance,
                             char **result_out);
extern int harness_world_export_json(unsigned long long handle, char **result_out);
extern int harness_world_close(unsigned long long handle);

static const char *FIXTURE =
    "The Hollow Keep\n\n"
    "## The Keeper\n\n"
    "Keeper Sarn guards the Hollow Keep. "
    "The Silver Key is hidden in the Hall of Embers. "
    "Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n"
    "## The Garden of Ash\n\n"
    "The Garden of Ash lies beyond the Iron Gate. "
    "The Deep Well stands at the center of the Garden of Ash.";

static int failures = 0;

static void check(int ok, const char *what) {
    printf("%s: %s\n", ok ? "CHECK PASS" : "CHECK FAIL", what);
    if (!ok) failures++;
}

static char *last_err(void) {
    char *e = harness_last_error();
    char *copy = strdup(e ? e : "(null)");
    harness_string_free(e);
    return copy;
}

int main(void) {
    char dir[256];
    char source_path[320], out_dir[320], state_root[320];
    unsigned long long handle = 0;
    char *result = NULL;
    int rc;

    snprintf(dir, sizeof dir, "/tmp/hdoor-ffi-proof-%ld", (long)getpid());
    snprintf(source_path, sizeof source_path, "%s/world.txt", dir);
    snprintf(out_dir, sizeof out_dir, "%s/package", dir);
    snprintf(state_root, sizeof state_root, "%s/state", dir);
    if (mkdir(dir, 0755) != 0) { printf("cannot mkdir %s\n", dir); return 1; }

    /* prepare source file */
    FILE *f = fopen(source_path, "w");
    if (!f) { printf("cannot write %s\n", source_path); return 1; }
    fputs(FIXTURE, f);
    fclose(f);

    check(harness_abi_version() == 1, "abi version == 1");

    char *ver = harness_version();
    printf("version: %s\n", ver);
    check(strstr(ver, "harness-ffi") != NULL, "version string present");
    harness_string_free(ver);

    rc = harness_world_compile(source_path, "ffi-proof-world", "FFI Proof World", out_dir);
    if (rc != 0) printf("compile error: %s\n", last_err());
    check(rc == 0, "world compile");

    rc = harness_world_open(out_dir, state_root, "i1", "main", &handle);
    if (rc != 0) printf("open error: %s\n", last_err());
    check(rc == 0 && handle != 0, "session open (handle non-zero)");

    rc = harness_world_act(handle, "go to the Hall of Embers", &result);
    if (rc != 0) printf("act error: %s\n", last_err());
    check(rc == 0 && strstr(result ? result : "", "\"ok\":true") != NULL, "act go");
    if (result) { harness_string_free(result); result = NULL; }

    rc = harness_world_act(handle, "take the Silver Key", &result);
    check(rc == 0 && strstr(result ? result : "", "\"ok\":true") != NULL, "act take");
    if (result) { harness_string_free(result); result = NULL; }

    rc = harness_world_act(handle, "ask keeper sarn about the bronze key", &result);
    check(rc == 0 && strstr(result ? result : "", "Sarn") != NULL, "act talk (Sarn replies)");
    if (result) { harness_string_free(result); result = NULL; }

    rc = harness_world_export_json(handle, &result);
    check(rc == 0 && strstr(result ? result : "", "hdoor_export_v1") != NULL,
          "signed export JSON (schema v1)");
    if (result) { harness_string_free(result); result = NULL; }

    rc = harness_world_close(handle);
    check(rc == 0, "session close");

    /* stale handle must fail cleanly — no crash */
    rc = harness_world_close(handle);
    check(rc != 0, "stale handle fails cleanly");

    printf("FFI PROOF: %s\n", failures == 0 ? "PASS" : "FAIL");
    return failures == 0 ? 0 : 1;
}