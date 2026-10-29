# Setting up Enable Banking for Otto

Read when a finance call says finance.toml or the key is missing. Walk the user through these steps in order, in French; the user does them, Otto cannot.

Enable Banking (enablebanking.com) is a PSD2 aggregator: the user registers their own application there, links their own bank accounts to it, and Otto talks to the API with that application's key. Without a signed contract the application runs in "restricted" production mode: it can only fetch the accounts the user linked in the panel, which is exactly what we want. Consent lasts 180 days for most banks, then the bank is reconnected the same way.

1. **Create the account and the application.** On enablebanking.com, "Sign in" / "Get started" (self-service), then the control panel → register an application:
   - name: `Otto` (or anything);
   - whitelisted redirect URLs: the `redirect_url` of finance.toml, by default `https://otto.taila4a29b.ts.net/connected.html` (the bank sends the user back there; Otto only needs the address the user lands on);
   - environment: production; production registration also asks for an application description (shown to the user during consent), a data-protection contact email (the user's own) and a privacy policy URL (any page of the user's, it is personal use).
   The browser then generates the application's private key and saves it to the downloads folder: a `.pem` file named after the application ID (a UUID). That ID is what finance.toml calls `app_id`. (One can instead generate the key with OpenSSL, `openssl genrsa -out finance.pem 2048`, and supply only the public key, `openssl rsa -in finance.pem -pubout`.)
2. **Link the user's own accounts** in the panel (restricted mode: the application can only read those).
3. **Save the key on the box**:
   ```
   mkdir -p ~/.config/otto
   mv ~/Downloads/<application id>.pem ~/.config/otto/finance.pem
   chmod 600 ~/.config/otto/finance.pem
   ```
4. **Write `~/.config/otto/finance.toml`**:
   ```toml
   app_id = "<application id (UUID)>"
   key = "~/.config/otto/finance.pem"
   redirect_url = "https://otto.taila4a29b.ts.net/connected.html"   # the whitelisted one
   household = ["Jean-Baptiste", "Marie"]                            # for shared expenses
   # psu_type = "personal"                                           # default
   # [accounts."<account uid>"]                                      # who owns an account, for shared expenses
   # owner = "Marie"
   ```
5. **Restart Otto**: `systemctl --user restart otto`.
6. **Connect a bank**: "connecte ma banque Fortuneo" → `finance.connect {bank: "Fortuneo"}` gives a link; the user opens it on the phone, logs in at the bank and accepts; the bank lands on `connected.html` (a page that may show nothing: that is fine); the user copies that page's full address and pastes it to Otto → `finance.connected {url}` → then `finance.sync {}`.

Troubleshooting: `REDIRECT_URI_NOT_ALLOWED` means the redirect URL in finance.toml isn't whitelisted in the panel exactly; `openssl n'a pas pu signer` means the .pem isn't the application's private key; a bank missing from `finance.banks` isn't covered by Enable Banking in France.
