"""Tests of annonce.py, against a fake GitHub and a fake Discord on 127.0.0.1 (nothing leaves the machine).
Run before every announcement by the workflow:  python3 -m unittest discover -s .github/annonce -v
"""
import hashlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import annonce  # noqa: E402

INSTALLEUR = b"MZ un faux installeur" * 1000
EMPREINTE = hashlib.sha256(INSTALLEUR).hexdigest().upper()
SALON_ID = "123456789012345678"


class Faux(http.server.BaseHTTPRequestHandler):
    """The fake GitHub (GET of the installer) and the fake Discord (the webhooks)."""
    recus = []
    vus = []      # every GET: (path, Authorization)
    release = None
    statut = 200

    def log_message(self, *a):
        pass

    def _repondre(self, statut, corps=b"", type_="application/json"):
        self.send_response(statut)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        Faux.vus.append((self.path, self.headers.get("Authorization")))
        if self.path == "/api/repos/SpottedBush/gradatim-releases/releases/tags/v0.3.2" and Faux.release:
            self._repondre(200, json.dumps(Faux.release).encode())
        elif self.path == "/telechargement/Gradatim-0.3.2-setup.exe":
            self._repondre(200, INSTALLEUR, "application/octet-stream")
        elif self.path == "/redirige":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/")
            self.end_headers()
        else:
            self._repondre(404)

    def _ecrire(self):
        n = int(self.headers.get("Content-Length") or 0)
        corps = json.loads(self.rfile.read(n))
        Faux.recus.append((self.command, self.path, corps, self.headers.get("User-Agent")))
        if Faux.statut != 200:
            return self._repondre(Faux.statut)
        if self.path.startswith("/redirige"):
            self.send_response(302)
            self.send_header("Location", "/suivi")  # on this same server: a client that follows it would GET it
            self.end_headers()
            return None
        self._repondre(200, json.dumps({"id": str(100000000000000000 + len(Faux.recus))}).encode())
        return None

    do_POST = do_PATCH = _ecrire


class Annonce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.serveur = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Faux)
        cls.base = f"http://127.0.0.1:{cls.serveur.server_address[1]}"
        threading.Thread(target=cls.serveur.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.serveur.shutdown()

    def setUp(self):
        Faux.recus, Faux.vus, Faux.statut, Faux.release = [], [], 200, None
        self.dossier = tempfile.TemporaryDirectory()
        self.addCleanup(self.dossier.cleanup)

    def release(self, **autres):
        r = {"tag_name": "v0.3.2", "name": "Gradatim 0.3.2", "draft": False, "prerelease": False,
             "html_url": "https://github.com/SpottedBush/gradatim-releases/releases/tag/v0.3.2",
             "body": f"Les nouveautés.\n\nL'empreinte doit être `{EMPREINTE}`.",
             "assets": [{"name": "Gradatim-0.3.2-setup.exe",
                         "browser_download_url": self.base + "/telechargement/Gradatim-0.3.2-setup.exe"},
                        {"name": "maj.json", "browser_download_url": self.base + "/telechargement/maj.json"}]}
        r.update(autres)
        return r

    def lancer(self, event, **env):
        chemin = Path(self.dossier.name) / "event.json"
        chemin.write_text(json.dumps(event), encoding="utf-8")
        e = {"GITHUB_EVENT_PATH": str(chemin), "DISCORD_NOTES": self.base + "/webhooks/notes",
             "DISCORD_DEMARRER": self.base + "/webhooks/demarrer"}
        e.update(env)
        sortie = io.StringIO()
        with redirect_stdout(sortie):
            code = annonce.main({k: v for k, v in e.items() if v is not None})
        return code, sortie.getvalue()

    # ------------------------------------------------------------ a published release

    def test_premiere_release_poste_notes_et_demarrer(self):
        code, sortie = self.lancer({"release": self.release()}, SALONS=json.dumps({"annonces": SALON_ID}))
        self.assertEqual(code, 0)
        self.assertIn(EMPREINTE, sortie)
        *demarrer, (m1, c1, notes, agent) = Faux.recus  # #démarrer first, the notes last
        self.assertEqual((m1, c1), ("POST", "/webhooks/notes?wait=true"))
        self.assertTrue(agent.startswith("DiscordBot ("))
        self.assertEqual(notes["allowed_mentions"], {"parse": []})
        self.assertIn(EMPREINTE, notes["embeds"][0]["description"])
        self.assertIn("0.3.2", notes["content"])
        self.assertEqual([(m, c) for m, c, _, _ in demarrer], [("POST", "/webhooks/demarrer?wait=true")] * 2)
        fr = demarrer[0][2]["content"]
        self.assertIn("Gradatim-0.3.2-setup.exe", fr)
        self.assertIn(EMPREINTE, fr)
        self.assertIn(f"<#{SALON_ID}>", fr)       # a channel whose id is known: its mention
        self.assertIn("#notes-de-version", fr)    # the others: their name
        self.assertNotIn("{", fr)
        self.assertIn("DEMARRER_MESSAGES", sortie)  # the ids to put in the variable

    def test_ensuite_demarrer_est_modifie(self):
        ids = "111111111111111111,222222222222222222"
        code, _ = self.lancer({"release": self.release()}, DEMARRER_MESSAGES=ids)
        self.assertEqual(code, 0)
        self.assertEqual([(m, c) for m, c, _, _ in Faux.recus[:2]],
                         [("PATCH", "/webhooks/demarrer/messages/111111111111111111"),
                          ("PATCH", "/webhooks/demarrer/messages/222222222222222222")])

    def test_empreinte_absente_des_notes_rien_n_est_poste(self):
        with self.assertRaisesRegex(annonce.Refus, "pas dans les notes"):
            self.lancer({"release": self.release(body="Les nouveautés, sans empreinte.")})
        self.assertEqual(Faux.recus, [])

    def test_une_autre_empreinte_dans_les_notes_refusee(self):
        autre = "F" * 64
        with self.assertRaisesRegex(annonce.Refus, "autre empreinte"):
            self.lancer({"release": self.release(body=f"`{EMPREINTE}` ou `{autre}`")})
        self.assertEqual(Faux.recus, [])

    def test_echec_au_second_message_le_premier_id_est_dit(self):
        reponses = iter([{"id": "333333333333333333"}])

        def envoyer(methode, adresse, corps):
            try:
                return next(reponses)
            except StopIteration:
                raise annonce.Refus("Discord répond 500 à un POST") from None
        sortie = io.StringIO()
        with redirect_stdout(sortie), self.assertRaises(annonce.Refus):
            annonce.publier_demarrer("x", ["a", "b"], [], envoyer=envoyer)
        self.assertIn("333333333333333333", sortie.getvalue())

    def test_lancement_a_la_main(self):
        Faux.release = self.release()
        env = {"GITHUB_API_URL": self.base + "/api", "GITHUB_REPOSITORY": "SpottedBush/gradatim-releases",
               "GITHUB_TOKEN": "jeton"}
        for notes, attendus in ((False, ["/webhooks/demarrer?wait=true"] * 2),
                                (True, ["/webhooks/demarrer?wait=true"] * 2 + ["/webhooks/notes?wait=true"])):
            with self.subTest(notes=notes):
                Faux.recus = []
                code, _ = self.lancer({"inputs": {"tag": "v0.3.2", "notes": notes}}, **env)
                self.assertEqual(code, 0)
                self.assertEqual([c for _, c, _, _ in Faux.recus], attendus)
        self.assertIn(("/api/repos/SpottedBush/gradatim-releases/releases/tags/v0.3.2", "Bearer jeton"), Faux.vus)
        with self.assertRaisesRegex(annonce.Refus, "GitHub répond 404"):
            self.lancer({"inputs": {"tag": "v9.9.9"}}, **env)

    def test_empreinte_en_minuscules_acceptee(self):
        code, _ = self.lancer({"release": self.release(body=f"`{EMPREINTE.lower()}`")})
        self.assertEqual(code, 0)

    def test_mauvais_reglage_rien_n_est_poste(self):
        for env in ({"DEMARRER_MESSAGES": "111111111111111111"}, {"DEMARRER_MESSAGES": "abc,def"},
                    {"SALONS": "pas du json"}, {"SALONS": "[1]"},
                    {"DISCORD_NOTES": "https://exemple.com/api/webhooks/1/2"}):
            with self.subTest(env=env), self.assertRaises(annonce.Refus):
                self.lancer({"release": self.release()}, **env)
            self.assertEqual(Faux.recus, [])

    def test_sans_installeur_refuse(self):
        with self.assertRaisesRegex(annonce.Refus, "pas de fichier"):
            self.lancer({"release": self.release(assets=[])})

    def test_installeur_ailleurs_refuse(self):
        a = [{"name": "Gradatim-0.3.2-setup.exe", "browser_download_url": "https://exemple.com/Gradatim.exe"}]
        with self.assertRaisesRegex(annonce.Refus, "inattendue"):
            self.lancer({"release": self.release(assets=a)})

    def test_brouillon_et_preversion_rien(self):
        for autre in ({"draft": True}, {"prerelease": True}):
            with self.subTest(autre=autre):
                code, sortie = self.lancer({"release": self.release(**autre)})
                self.assertEqual(code, 0)
        self.assertEqual(Faux.recus, [])

    def test_sans_secrets_rien_n_est_poste(self):
        code, sortie = self.lancer({"release": self.release()}, DISCORD_NOTES=None, DISCORD_DEMARRER=None)
        self.assertEqual(code, 0)
        self.assertEqual(Faux.recus, [])
        self.assertIn("DISCORD_NOTES absent", sortie)

    def test_discord_refuse_l_adresse_n_est_pas_dite(self):
        Faux.statut = 401
        with self.assertRaises(annonce.Refus) as e:
            self.lancer({"release": self.release()})
        self.assertIn("401", str(e.exception))
        self.assertNotIn("webhooks", str(e.exception))

    def test_pas_de_redirection_vers_discord(self):
        with self.assertRaisesRegex(annonce.Refus, "302"):
            annonce.discord("POST", self.base + "/redirige", {"content": "x"})
        self.assertEqual(len(Faux.recus), 1)
        self.assertNotIn("/suivi", [p for p, _ in Faux.vus])

    # ------------------------------------------------------------ the texts

    def test_notes_trop_longues_coupees(self):
        m = annonce.message_notes(self.release(body="x" * 5000 + EMPREINTE, name="T" * 300), "0.3.2", EMPREINTE)
        d = m["embeds"][0]["description"]
        self.assertLessEqual(len(d), annonce.DESCRIPTION_MAX)
        self.assertIn("la suite sur GitHub", d)
        self.assertTrue(d.endswith(f"`{EMPREINTE}`"))  # the fingerprint cut off is written again
        self.assertEqual(len(m["embeds"][0]["title"]), annonce.TITRE_MAX)

    def test_le_vrai_modele_tient_dans_discord(self):
        modele = json.loads(annonce.MODELE.read_text(encoding="utf-8"))
        textes = annonce.rendre_demarrer(modele, "Gradatim-10.10.10-setup.exe", "A" * 64,
                                         {c: SALON_ID for c in modele["noms"]})
        self.assertEqual(len(textes), 2)
        for t in textes:
            self.assertLessEqual(len(t), annonce.MESSAGE_MAX)
            self.assertIn("A" * 64, t)
            self.assertNotIn("{", t)

    def test_lancement_a_la_main_tag_verifie(self):
        for tag in ("", "0.3.2", "v0.3.2; rm -rf /", "v0.3.2/../x"):
            with self.subTest(tag=tag), self.assertRaisesRegex(annonce.Refus, "tag"):
                annonce.lire_release({"inputs": {"tag": tag, "notes": "true"}}, {})

    def test_salon_inconnu_refuse(self):
        with self.assertRaisesRegex(annonce.Refus, "inconnu"):
            annonce.rendre_demarrer({"messages": ["{#secret}"], "noms": {}}, "a", "b", {})


if __name__ == "__main__":
    unittest.main()
