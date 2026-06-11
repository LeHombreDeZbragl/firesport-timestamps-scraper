Now i want to change some logic.
I dont want to scrape certain categories, because they are different type than those that i want to keep and work with. 
Add blacklisted_categories (or more fitting name if appropriate) and there you should put not exact categories, but it will work like this: it will go to the line from which the categories are extracted, and it will check the whole line for the occurance of these words - if any are detected, it will not collect this competition (and write some notice/warning). The words are these: (case insensitive here) "plamen", "60", "Děti-mladší", "Děti-starší", "Přípravka", "jednotlivec", "štafeta", "mčr", "ctif", "věž", "kombinace", "starší", "mladší", "střední", "mladí-hasiči", "štafeta", "dle pravidel dorost"

Also i want to save only save few categories - right now there is about 20 and its just too much - i want to create new section in the config that will map certain categories to "Ostatní" - but to "Ostatní" category not type. These categories should map to the Ostatni category: "ženy>30", "finále", "profi", "ps-12", "volné", "dospelí", "smíšené"

at the end make sure that readme and claude are consistent with the changes you made.

create plan for these refactors/features.