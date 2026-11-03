#!/usr/bin/env bash
# Add the retailers that post on Workday to the server's board list. Run it once after deploying (it is safe to run again: a board already
# there is kept). Each retailer's robots.txt allows reading its Workday site; Lowe's, Home Depot, Wegmans, Nike and others block theirs and
# are read from their own career sites instead (see retail_sources.py). --retail also searches each with `retail_titles` in config.yaml.
set -uo pipefail
cd "$(dirname "$0")/.."

add() {   # add <name> <careers page address>
    echo "== $1"
    docker compose run --rm app python main.py workday-add "$2" --name "$1" --retail || echo "   not added: $1"
}

add "Target"                "https://target.wd5.myworkdayjobs.com/targetcareers"
add "Nordstrom"             "https://nordstrom.wd501.myworkdayjobs.com/nordstrom_careers"     # Nordstrom Rack is on the same site
add "Hy-Vee"                "https://hyvee.wd1.myworkdayjobs.com/HyVeeCareers"
add "BJ's Wholesale Club"   "https://bjswholesaleclub.wd1.myworkdayjobs.com/BJsCareers"
add "Meijer"                "https://meijer.wd5.myworkdayjobs.com/Meijer"
add "Les Schwab"            "https://lesschwab.wd1.myworkdayjobs.com/HQ"
add "Abercrombie & Fitch"   "https://abercrombie.wd108.myworkdayjobs.com/en-US/anf"            # also Hollister
add "Levi Strauss & Co."    "https://levistraussandco.wd5.myworkdayjobs.com/External"
add "Petco"                 "https://petco.wd504.myworkdayjobs.com/External"
add "Ace Hardware"          "https://acehardware.wd1.myworkdayjobs.com/External"
add "Crate & Barrel"        "https://crateandbarrel.wd1.myworkdayjobs.com/CBH"                # also CB2
add "World Market"          "https://worldmarket.wd5.myworkdayjobs.com/cost_plus_world_market_careers"
add "Signet Jewelers"       "https://signetjewelers.wd1.myworkdayjobs.com/SignetCorporateCareers"
add "Kay Jewelers"          "https://signetjewelers.wd1.myworkdayjobs.com/en-US/Kay_Jewelers"
add "Zales"                 "https://signetjewelers.wd1.myworkdayjobs.com/signetjeweleryretailsales"
add "Jared"                 "https://signetjewelers.wd1.myworkdayjobs.com/Jared_Jewelers"
add "Dick's Sporting Goods" "https://dickssportinggoods.wd1.myworkdayjobs.com/DSG"
add "RaceTrac"              "https://racetrac.wd5.myworkdayjobs.com/SSC"
add "Cracker Barrel"        "https://cbrlgroup.wd503.myworkdayjobs.com/CrackerBarrelExternal"

echo "Done. The next fetch reads them: docker compose run --rm app python main.py fetch"
