"""Announces a release of Gradatim on its Discord, from the workflow annonce-discord.yml (decided by Vincent on
2026-11-22; coach_lol: produit/discord-beta.md, section 6).

At each published release:
1. it downloads the release's installer and computes its SHA-256;
2. it refuses to announce anything when that fingerprint is not in the release's notes: the notes and #démarrer are
   the two places a tester checks it, and neither may show a fingerprint that is not the installer's;
3. it posts the notes in #notes-de-version (secret DISCORD_NOTES, the channel's webhook);
4. it rewrites the messages of #démarrer (secret DISCORD_DEMARRER) from demarrer.json ({installeur} and {empreinte}
   would be the installer's name and fingerprint; since 2026-11-24 the text is fixed and has neither). A webhook only edits its own messages: the first run posts them and prints their ids, which
   Vincent puts in the variable DEMARRER_MESSAGES (« id1,id2 »); every run after that edits them.

The variable SALONS ({"annonces": "<id>", ...}, optional) turns {#cle} into the channel's mention; without an id the
text says #nom. A webhook's address is a secret: it is never printed. Standard library only, no dependency.
"""
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ICI = Path(__file__).resolve().parent
MODELE = ICI / "demarrer.json"
AGENT = "DiscordBot (https://gradatim.gg, 1)"  # the User-Agent form Discord asks of every client of its API
WEBHOOK = re.compile(r"https://discord\.com/api/webhooks/[0-9]{15,22}/[A-Za-z0-9_-]{20,200}"
                     r"|http://127\.0\.0\.1:[0-9]{2,5}/[A-Za-z0-9/_-]{1,200}")  # 127.0.0.1: the tests only
TELECHARGEMENT = re.compile(r"https://github\.com/SpottedBush/gradatim-releases/releases/download/[A-Za-z0-9._/-]+"
                            r"|http://127\.0\.0\.1:[0-9]{2,5}/[A-Za-z0-9._/-]+")
TAG = re.compile(r"v([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})")
ID = re.compile(r"[0-9]{15,22}")
DESCRIPTION_MAX = 4096  # an embed's description (Discord, « Embed Limits »)
TITRE_MAX = 256         # an embed's title
MESSAGE_MAX = 2000      # a message's content
INSTALLEUR_MAX = 200 * 1024 * 1024
COULEUR = 0x3BA55C
ATTENTE_S = 30


class Refus(Exception):
    """Nothing more is announced, and the job fails with this sentence."""


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a webhook's message goes to its address and nowhere else
        return None


_vers_discord = urllib.request.build_opener(_SansRedirection).open


def webhook(env, nom):
    """The webhook of the secret `nom`, None when it is not set. Raises on an address of another form."""
    adresse = (env.get(nom) or "").strip()
    if not adresse:
        return None
    if not WEBHOOK.fullmatch(adresse):
        raise Refus(f"{nom} : https://discord.com/api/webhooks/<id>/<jeton> attendu")
    return adresse


def discord(methode, adresse, corps):
    """One call to a webhook; returns the message Discord gives back. The address never appears in an error."""
    requete = urllib.request.Request(adresse, method=methode, data=json.dumps(corps, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": AGENT})
    try:
        with _vers_discord(requete, timeout=ATTENTE_S) as r:
            texte = r.read()
    except urllib.error.HTTPError as e:
        raise Refus(f"Discord répond {e.code} à un {methode}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Refus(f"Discord injoignable ({type(e).__name__})") from None
    return json.loads(texte) if texte else {}


def lire_release(event, env):
    """The release to announce, and whether its notes are posted: the event's on a publication, the one the run
    names on a manual run (« tag »; « notes » says whether the notes are posted again)."""
    if "release" in event:
        return event["release"], True
    entrees = event.get("inputs") or {}
    tag = str(entrees.get("tag") or "")
    if not TAG.fullmatch(tag):
        raise Refus("tag : v<x>.<y>.<z> attendu")
    api = (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")  # set by GitHub Actions
    adresse = f"{api}/repos/{env['GITHUB_REPOSITORY']}/releases/tags/{tag}"
    requete = urllib.request.Request(adresse, headers={"Accept": "application/vnd.github+json",
                                                       "Authorization": f"Bearer {env['GITHUB_TOKEN']}",
                                                       "User-Agent": "gradatim-annonce"})
    try:
        with urllib.request.urlopen(requete, timeout=ATTENTE_S) as r:
            return json.load(r), str(entrees.get("notes")).lower() == "true"
    except urllib.error.HTTPError as e:
        raise Refus(f"GitHub répond {e.code} pour la release {tag}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Refus(f"GitHub injoignable ({type(e).__name__})") from None


def installeur(release):
    """(name, address) of the release's installer, Gradatim-<version>-setup.exe."""
    m = TAG.fullmatch(release.get("tag_name") or "")
    if not m:
        raise Refus(f"tag {release.get('tag_name')!r} : v<x>.<y>.<z> attendu")
    nom = f"Gradatim-{m.group(1)}-setup.exe"
    for a in release.get("assets") or []:
        if a.get("name") == nom:
            adresse = a.get("browser_download_url") or ""
            if not TELECHARGEMENT.fullmatch(adresse):
                raise Refus(f"{nom} : adresse de téléchargement inattendue")
            return nom, adresse
    raise Refus(f"la release n'a pas de fichier {nom}")


def empreinte(adresse, ouvrir=urllib.request.urlopen):
    """The SHA-256 of the file at `adresse`, in capitals as Get-FileHash prints it."""
    h, taille = hashlib.sha256(), 0
    with ouvrir(urllib.request.Request(adresse, headers={"User-Agent": "gradatim-annonce"}), timeout=120) as r:
        while bloc := r.read(1 << 20):
            taille += len(bloc)
            if taille > INSTALLEUR_MAX:
                raise Refus("installeur trop gros")
            h.update(bloc)
    return h.hexdigest().upper()


def verifier_notes(notes, valeur):
    """Raises when the notes do not carry the installer's fingerprint (in capitals or not), or carry another
    SHA-256 beside it: a tester must never read two fingerprints."""
    trouvees = {x.upper() for x in re.findall(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])", notes or "")}
    if valeur not in trouvees:
        raise Refus(f"l'empreinte de l'installeur ({valeur}) n'est pas dans les notes de la release : rien n'est "
                    "annoncé. Corrige les notes, puis relance l'action à la main (tag, notes cochées).")
    if trouvees != {valeur}:
        raise Refus("les notes de la release portent une autre empreinte que celle de l'installeur : rien n'est "
                    "annoncé. Corrige les notes, puis relance l'action à la main (tag, notes cochées).")


def message_notes(release, version, valeur):
    """The message of #notes-de-version: the notes as they are, in an embed that links to the release. Notes too
    long for it are cut, and the installer's fingerprint is then written again at the end."""
    notes = (release.get("body") or "").strip()
    if len(notes) > DESCRIPTION_MAX:
        suite = f"\n\n… la suite sur GitHub. Empreinte de l'installeur : `{valeur}`"
        notes = notes[:DESCRIPTION_MAX - len(suite)].rstrip() + suite
    titre = (release.get("name") or f"Gradatim {version}")[:TITRE_MAX]
    return {"content": f"**Gradatim {version}** est sortie.",
            "embeds": [{"title": titre, "url": release.get("html_url"),
                        "description": notes, "color": COULEUR}],
            "allowed_mentions": {"parse": []}}


def rendre_demarrer(modele, nom, valeur, salons):
    """The texts of #démarrer for this installer. {#cle} is the channel's mention when SALONS gives its id."""
    def salon(m):
        cle = m.group(1)
        if cle not in modele["noms"]:
            raise Refus(f"salon {{#{cle}}} inconnu du modèle")
        ident = str(salons.get(cle) or "")
        return f"<#{ident}>" if ID.fullmatch(ident) else "#" + modele["noms"][cle]
    textes = []
    for m in modele["messages"]:
        t = re.sub(r"\{#([a-z]+)\}", salon, m.replace("{installeur}", nom).replace("{empreinte}", valeur))
        if len(t) > MESSAGE_MAX:
            raise Refus(f"un message de #démarrer fait {len(t)} caractères, plus que les {MESSAGE_MAX} de Discord")
        textes.append(t)
    return textes


def lire_ids(ids_texte, n):
    """The ids of DEMARRER_MESSAGES: [] when it is empty, else exactly `n` message ids."""
    ids = [i.strip() for i in (ids_texte or "").split(",") if i.strip()]
    if ids and (len(ids) != n or not all(ID.fullmatch(i) for i in ids)):
        raise Refus(f"DEMARRER_MESSAGES : {n} identifiants de message attendus, séparés par une virgule")
    return ids


def publier_demarrer(adresse, textes, ids, envoyer=discord):
    """Edits the messages `ids` of #démarrer, or posts them when there is none yet. Returns their ids."""
    corps = [{"content": t, "allowed_mentions": {"parse": []}} for t in textes]
    if not ids:
        nouveaux = []
        for c in corps:  # each id printed as soon as Discord gives it: a failure on the next one loses none
            nouveaux.append(str(envoyer("POST", adresse + "?wait=true", c)["id"]))
            print(f"#démarrer : message {len(nouveaux)} posté, identifiant {nouveaux[-1]}")
        print("#démarrer : mets la variable DEMARRER_MESSAGES du dépôt à « " + ",".join(nouveaux)
              + " », puis supprime l'ancien #démarrer dans Discord.")
        return nouveaux
    for i, c in zip(ids, corps):
        envoyer("PATCH", f"{adresse}/messages/{i}", c)
    print(f"#démarrer : {len(ids)} message(s) mis à jour.")
    return ids


def main(env=os.environ, ouvrir=urllib.request.urlopen):
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    release, poster_notes = lire_release(event, env)
    if release.get("draft") or release.get("prerelease"):
        print("Brouillon ou préversion : rien n'est annoncé.")
        return 0
    notes_adresse, demarrer_adresse = webhook(env, "DISCORD_NOTES"), webhook(env, "DISCORD_DEMARRER")
    nom, adresse = installeur(release)
    valeur = empreinte(adresse, ouvrir)
    print(f"{nom} : {valeur}")
    verifier_notes(release.get("body"), valeur)
    version = TAG.fullmatch(release["tag_name"]).group(1)
    textes = None
    if demarrer_adresse:  # read and checked before anything is posted: a wrong setting posts nothing
        try:
            salons = json.loads(env.get("SALONS") or "{}")
        except ValueError:
            raise Refus("SALONS : un objet JSON attendu, {\"annonces\": \"<id>\", ...}") from None
        if not isinstance(salons, dict):
            raise Refus("SALONS : un objet JSON attendu, {\"annonces\": \"<id>\", ...}")
        textes = rendre_demarrer(json.loads(MODELE.read_text(encoding="utf-8")), nom, valeur, salons)
        ids = lire_ids(env.get("DEMARRER_MESSAGES"), len(textes))
    # #démarrer first: a run that fails after it can be started again by hand without « notes », and the notes
    # never announce a version that #démarrer does not show yet
    if textes is not None:
        publier_demarrer(demarrer_adresse, textes, ids)
    else:
        print("#démarrer : secret DISCORD_DEMARRER absent, rien n'est modifié.")
    if not poster_notes:
        print("#notes-de-version : pas reposté (lancement à la main sans « notes »).")
    elif notes_adresse:
        discord("POST", notes_adresse + "?wait=true", message_notes(release, version, valeur))
        print("#notes-de-version : notes postées.")
    else:
        print("#notes-de-version : secret DISCORD_NOTES absent, rien n'est posté.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except Refus as e:
        print(f"Refusé : {e}")
        print("Une fois corrigé : Actions › Annonce Discord › Run workflow, avec le tag ; « notes » coché seulement si "
              "les notes ne sont pas déjà dans #notes-de-version.")
        sys.exit(1)
