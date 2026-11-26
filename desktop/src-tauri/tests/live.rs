//! Checks against the real GitHub and the agents on this Mac. They need the
//! network and the person's own sign-ins, so they run only when asked:
//! `cargo test --test live -- --ignored --nocapture`.

use std::process::Command;

fn search() -> String {
    let login = Command::new(std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into()))
        .args(["-l", "-c", "printf %s \"$PATH\""])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default();
    format!(
        "{login}:/usr/bin:/bin:{}/.local/bin",
        std::env::var("HOME").unwrap()
    )
}

#[test]
#[ignore]
fn finds_this_macs_github_sign_in() {
    let session = codenib_desktop::github_session();
    let status = codenib_desktop::github_status_for_test(&session, &search());
    println!(
        "signed_in={} login={:?} source={:?} problem={:?}",
        status.signed_in, status.login, status.source, status.problem
    );
    assert!(status.signed_in, "expected a GitHub sign-in on this Mac");
}

#[test]
#[ignore]
fn looks_up_a_public_repository_without_signing_in() {
    let repo =
        codenib_desktop::github_lookup_for_test("https://github.com/psf/requests", None).unwrap();
    assert_eq!(repo, ("psf/requests".to_owned(), false));
    let missing = codenib_desktop::github_lookup_for_test("psf/no-such-repository-codenib", None);
    assert!(missing.unwrap_err().contains("not found"));
}

#[test]
#[ignore]
fn clones_a_repository_and_reuses_it() {
    let dir = std::env::temp_dir().join(format!("codenib-clone-{}", std::process::id()));
    let target = dir.join("octocat/Hello-World");
    let mut lines = Vec::new();
    codenib_desktop::clone_for_test("octocat/Hello-World", &target, None, &search(), |l| {
        lines.push(l)
    })
    .unwrap();
    assert!(target.join(".git").is_dir());
    assert!(!lines.is_empty(), "git progress reaches the window");
    // A second clone of the same repository reuses the folder.
    codenib_desktop::clone_for_test("octocat/Hello-World", &target, None, &search(), |_| {})
        .unwrap();
    // Another repository is refused there.
    let other = codenib_desktop::clone_for_test("psf/requests", &target, None, &search(), |_| {});
    assert!(other.unwrap_err().contains("different repository"));
    // No token was written into the clone's configuration.
    let config = std::fs::read_to_string(target.join(".git/config")).unwrap();
    assert!(!config.to_lowercase().contains("authorization"));
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
#[ignore]
fn reads_the_agents_sign_in() {
    for agent in codenib_desktop::agents_for_test(&search()) {
        println!("{agent}");
    }
}

#[test]
#[ignore]
fn clones_a_private_repository_with_this_macs_sign_in() {
    let session = codenib_desktop::github_session();
    let token =
        codenib_desktop::github_token_for_test(&session, &search()).expect("a GitHub sign-in");
    let repos = codenib_desktop::github_repositories_for_test(&token).unwrap();
    let (slug, _) = repos
        .iter()
        .find(|(_, private)| *private)
        .expect("a private repository")
        .clone();
    assert_eq!(
        codenib_desktop::github_lookup_for_test(&slug, Some(&token)).unwrap(),
        (slug.clone(), true)
    );
    // Signed out, GitHub hides it, and the message says to sign in.
    let hidden = codenib_desktop::github_lookup_for_test(&slug, None).unwrap_err();
    assert!(hidden.contains("sign in"), "{hidden}");
    let dir = std::env::temp_dir().join(format!("codenib-private-{}", std::process::id()));
    let target = dir.join(&slug);
    // No credential helper: only the token CodeNib hands git can work.
    std::env::set_var("GIT_CONFIG_GLOBAL", "/dev/null");
    std::env::set_var("GIT_CONFIG_NOSYSTEM", "1");
    let refused =
        codenib_desktop::clone_for_test(&slug, &dir.join("signed-out"), None, &search(), |_| {});
    assert!(
        refused.unwrap_err().contains("sign in"),
        "a signed-out clone explains itself"
    );
    codenib_desktop::clone_for_test(&slug, &target, Some(&token), &search(), |_| {}).unwrap();
    assert!(target.join(".git").is_dir());
    let config = std::fs::read_to_string(target.join(".git/config")).unwrap();
    assert!(!config.contains(&token) && !config.to_lowercase().contains("authorization"));
    println!("cloned private {slug}");
    std::fs::remove_dir_all(dir).unwrap();
    std::env::remove_var("GIT_CONFIG_GLOBAL");
    std::env::remove_var("GIT_CONFIG_NOSYSTEM");
}

#[test]
#[ignore]
fn lists_the_signed_in_persons_repositories() {
    let session = codenib_desktop::github_session();
    let token =
        codenib_desktop::github_token_for_test(&session, &search()).expect("a GitHub sign-in");
    let repos = codenib_desktop::github_repositories_for_test(&token).unwrap();
    let private: Vec<_> = repos.iter().filter(|(_, p)| *p).collect();
    println!(
        "{} repositories, {} private; first private: {:?}",
        repos.len(),
        private.len(),
        private.first()
    );
    assert!(!repos.is_empty());
}
