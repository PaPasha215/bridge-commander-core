/* Bridge Commander.app launcher: keeps the app as the parent (and so the process macOS privacy
   settings are granted to) of `python3 bridge-commander.pyz remote`. Exits 0 when this Mac is not
   signed in to a cloud, so launchd does not restart it after a revoke. */
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child;

static void forward(int sig) { if (child > 0) kill(child, sig); }

int main(void) {
    char exe[PATH_MAX], real[PATH_MAX], pyz[PATH_MAX], creds[PATH_MAX];
    uint32_t size = sizeof exe;
    const char *home = getenv("HOME");
    struct stat st;
    if (!home || _NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) return 1;
    char *slash = strrchr(real, '/');               /* .../Contents/MacOS/<exe> */
    if (!slash) return 1;
    *slash = 0;
    snprintf(pyz, sizeof pyz, "%s/../Resources/bridge-commander.pyz", real);
    snprintf(creds, sizeof creds, "%s/.config/bridge-commander/device.json", home);
    if (stat(creds, &st) != 0) {
        fprintf(stderr, "bridge-commander: not signed in (%s missing); run `remote` once in a terminal\n", creds);
        return 0;
    }
    char *argv[] = {"/usr/bin/python3", "-I", pyz, "remote", "--no-browser", NULL};
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    int rc = posix_spawn(&child, argv[0], NULL, NULL, argv, environ);
    if (rc != 0) { fprintf(stderr, "bridge-commander: cannot start python3: %s\n", strerror(rc)); return 1; }
    int status;
    while (waitpid(child, &status, 0) < 0) if (errno != EINTR) return 1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
