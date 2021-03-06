#!/usr/bin/env python
"""Module to calculate pn ps."""

from __future__ import division
from Bio import AlignIO
from Bio.Align import MultipleSeqAlignment
from Bio.Data import CodonTable
from divergence import find_cogs_in_sequence_records, parse_options, create_directory, extract_archive_of_files, \
    concatenate, CODON_TABLE_ID, get_most_recent_gene_name
from divergence.run_codeml import run_codeml, parse_codeml_output
from divergence.run_phipack import run_phipack
from divergence.select_taxa import select_genomes_by_ids
from itertools import product
from operator import itemgetter
from random import choice
import logging as log
import os.path
import re
import shutil
import sys
import tempfile

__author__ = "Tim te Beek"
__contact__ = "brs@nbic.nl"
__copyright__ = "Copyright 2011, Netherlands Bioinformatics Centre"
__license__ = "MIT"


def _bootstrap(comp_values_list):
    """Bootstrap by gene to get to confidence scores for Neutrality Index."""

    def _sample_with_replacement(sample_set, sample_size=None):
        """Sample sample_size items from sample_set, or len(sample_set) items if sample_size is None (default)."""
        if sample_size is None:
            sample_size = len(sample_set)
        samples = []
        while len(samples) < sample_size:
            samples.append(choice(sample_set))
        return samples

    #"to get the confident interval on this you need to boostrap by gene - i.e. if we have 1000 genes, we form a
    #boostrap sample by resampling, with replacement 1000 genes from the original sample; recalculate NI and repeat
    #1000 times; the SE on the estimate is the standard deviation across bootstraps, and your 95% confidence
    #interval van be obtained by sorting the values and taking the 25t and 975th values"
    ni_values = []
    while len(ni_values) < len(comp_values_list):
        samples = _sample_with_replacement(comp_values_list)
        sum_dspn = sum(comp_values['Ds*Pn/(Ps+Ds)'] for comp_values in samples if comp_values['Ds*Pn/(Ps+Ds)'])
        sum_dnps = sum(comp_values['Dn*Ps/(Ps+Ds)'] for comp_values in samples if comp_values['Dn*Ps/(Ps+Ds)'])
        neutrality_index = sum_dspn / sum_dnps
        ni_values.append(neutrality_index)

    #95 percent of values fall between n*.025th element & n*.975th element when NI values are sorted
    ni_values = sorted(ni_values)
    lower_limit = int(round(0.025 * (len(ni_values) - 1)))
    upper_limit = int(round(0.975 * (len(ni_values) - 1)))
    return ni_values[lower_limit], ni_values[upper_limit]


def _append_sums_and_dos_average(calculations_file, sfs_max_nton, comp_values_list):
    """Append sums over columns 3 through -1, and the mean of the final direction of selection column."""
    sum_comp_values = {}
    dos_list = []
    for comp_values in comp_values_list:
        #Sum the following columns
        for column in _get_column_headers_in_sequence(sfs_max_nton)[4:-2]:
            if comp_values.get(column) is not None:
                old_value = sum_comp_values.get(column, 0)
                sum_comp_values[column] = old_value + comp_values[column]
        #Append value for DoS to list, so we can calculate the mean afterwards
        if comp_values['DoS'] is not None:
            dos_list.append(comp_values['DoS'])

    _append_statistics(calculations_file, '#sum', sum_comp_values, sfs_max_nton)

    #Calculate DoS average
    mean_values = dict((key, value / len(comp_values_list)) for key, value in sum_comp_values.iteritems())
    if dos_list:
        mean_values['DoS'] = sum(dos_list) / len(dos_list)
    _append_statistics(calculations_file, '#mean', mean_values, sfs_max_nton)

    #Neutrality Index = Sum(X = Ds*Pn/(Ps+Ds)) / Sum(Y = Dn*Ps/(Ps+Ds))
    if sum_comp_values['Dn*Ps/(Ps+Ds)']:
        neutrality_values = {'neutrality index': sum_comp_values['Ds*Pn/(Ps+Ds)'] / sum_comp_values['Dn*Ps/(Ps+Ds)']}
        _append_statistics(calculations_file, '#NI', neutrality_values, sfs_max_nton)
        #Find lower and upper limits within which 95% of values fall, by using bootstrapping statistics
        lower_95perc_limit, upper_95perc_limit = _bootstrap(comp_values_list)
        _append_statistics(calculations_file, '#NI 95% lower limit',
                           {'neutrality index': lower_95perc_limit}, sfs_max_nton)
        _append_statistics(calculations_file, '#NI 95% upper limit',
                           {'neutrality index': upper_95perc_limit}, sfs_max_nton)


def _every_other_codon_alignments(alignment):
    """Separate alignment into separate alignments per codon, to get independent axis when graphing data."""
    #Calculate sequence_length to use when splitting MSA into codons
    sequence_lengths = len(alignment[0]) - len(alignment[0]) % 3
    alignment_codons = [alignment[:, index:index + 3] for index in range(0, sequence_lengths, 3)]

    def _concat_codons_to_alignment(codons):
        """Concatenate codons as Bio.Align.MultipleSeqAlignment to one another to create a composed MSA."""
        alignment = codons[0]
        for codon in codons[1:]:
            alignment += codon
        return alignment

    #Odd alignments are the sum of the odd codons
    ali_odd = _concat_codons_to_alignment([codon for index, codon in enumerate(alignment_codons, 1) if index % 2 == 1])
    #Even alignments are the sum of the even codons
    ali_even = _concat_codons_to_alignment([codon for index, codon in enumerate(alignment_codons, 1) if index % 2 == 0])
    return ali_odd, ali_even


def _phipack_values_for_sicos(orth_files):
    """Calculate PhiPack values for each ortholog and return a dictionary mapping ortholog to the PhiPack values."""
    #Create temporary folder for PhiPack files
    phipack_dir = tempfile.mkdtemp(prefix='phipack_')
    values_per_orth = dict((ortholog, run_phipack(phipack_dir, sico_file)) for ortholog, sico_file in orth_files)
    #Remove phipack directory
    shutil.rmtree(phipack_dir)
    return values_per_orth


def calculate_tables(genome_ids_a, genome_ids_b, sico_files, oddeven=False):
    """Compute a spreadsheet of data points each for A and B based the SICO files, without duplicating computations."""
    #Convert file names into identifiers while preserving filenames, as filenames are used both for BioPython & PhiPack
    orth_files = [(os.path.split(sico_file)[1].split('.')[0], sico_file) for sico_file in sico_files]

    #Find PhiPack values for each sico file
    orth_phipack_values = _phipack_values_for_sicos(orth_files)

    #Convert list of sico files into ortholog name mapped to BioPython Alignment object
    sico_alignments = [(ortholog, AlignIO.read(sico_file, 'fasta'))
                       for ortholog, sico_file in orth_files]

    #Only retrieve genomes once which we'll use to link gene names to orthologs
    all_genome_ids = list(genome_ids_a)
    all_genome_ids.extend(genome_ids_b)
    genomes = select_genomes_by_ids(all_genome_ids).values()

    #For each ortholog, determine the newest gene name across taxa so unannotated taxa also get gene names
    ortholog_gene_names = dict((ortholog, get_most_recent_gene_name(genomes, alignmnt))
                               for ortholog, alignmnt in sico_alignments)

    #Split individual sico alignments into separate alignments for each of the clades per ortholog
    #These split alignments can later be reversed and/or subselections can be made to calculate for alternate alignments
    split_alignments = [(ortholog,
                         MultipleSeqAlignment(seqr for seqr in alignmnt if seqr.id.split('|')[0] in genome_ids_a),
                         MultipleSeqAlignment(seqr for seqr in alignmnt if seqr.id.split('|')[0] in genome_ids_b))
                        for ortholog, alignmnt in sico_alignments]

    #Calculate tables for normal sico alignments
    log.info('Starting calculations for full alignments')
    table_a, table_b = _tables_for_split_alignments(split_alignments, ortholog_gene_names, orth_phipack_values)

    if not oddeven:
        return table_a, table_b

    #As an alternate method of calculating number of substitutions for independent X-axis of eventual graph:
    #split each alignment for a and b into two further alignments of odd and even codons
    odd_even_split_orth_alignments = [(orthologname,
                                      _every_other_codon_alignments(alignment_x),
                                      _every_other_codon_alignments(alignment_y))
                                      for orthologname, alignment_x, alignment_y in split_alignments]

    #Recover odd alignments as first from each pair of alignments
    odd_split_alignments = [(orthologname,
                            odd_even_x[0],
                            odd_even_y[0])
                            for orthologname, odd_even_x, odd_even_y in odd_even_split_orth_alignments]

    #Create files for all the odd codon alignments, so we can run PhiPack for them
    odd_alignments_dir = tempfile.mkdtemp(prefix='odd_codon_alignments_')
    odd_files = dict((ortholog, os.path.join(odd_alignments_dir, ortholog + '.ffn'))
                     for ortholog, odd_x, odd_y in odd_split_alignments)
    for ortholog, odd_x, odd_y in odd_split_alignments:
        AlignIO.write([odd_x, odd_y], odd_files[ortholog], 'fasta')
    odd_phipack_vals = _phipack_values_for_sicos(odd_files.items())
    shutil.rmtree(odd_alignments_dir)

    #Calculate tables for odd codon sico alignments
    log.info('Starting calculations for odd alignments')
    table_a_odd, table_b_odd = _tables_for_split_alignments(odd_split_alignments, ortholog_gene_names, odd_phipack_vals)

    #Recover even alignments as second from each pair of alignments
    even_split_alignments = [(orthologname,
                            odd_even_x[1],
                            odd_even_y[1])
                            for orthologname, odd_even_x, odd_even_y in odd_even_split_orth_alignments]

    #Create files for all the odd codon alignments, so we can run PhiPack for them
    even_alignments_dir = tempfile.mkdtemp(prefix='even_codon_alignments_')
    even_files = dict((ortholog, os.path.join(even_alignments_dir, ortholog + '.ffn'))
                     for ortholog, even_x, even_y in even_split_alignments)
    for ortholog, even_x, even_y in even_split_alignments:
        AlignIO.write([even_x, even_y], even_files[ortholog], 'fasta')
    even_phipack_vals = _phipack_values_for_sicos(even_files.items())
    shutil.rmtree(even_alignments_dir)

    #Calculate tables for even codon sico alignments
    log.info('Starting calculations for even alignments')
    table_a_even, table_b_even = _tables_for_split_alignments(even_split_alignments,
                                                              ortholog_gene_names,
                                                              even_phipack_vals)

    #Concatenate tables and return their values
    table_a_full = tempfile.mkstemp(suffix='.tsv', prefix='table_a_full_')[1]
    table_b_full = tempfile.mkstemp(suffix='.tsv', prefix='table_b_full_')[1]
    concatenate(table_a_full, [table_a, table_a_odd, table_a_even])
    concatenate(table_b_full, [table_b, table_b_odd, table_b_even])
    return table_a_full, table_b_full


def _codeml_values_for_alignments(codeml_dir, ali_x, ali_y):
    """Calculate codeml values for sico files for full alignment, and alignments of even and odd codons."""
    #Run codeml to calculate values for dn & ds
    subdir = tempfile.mkdtemp(dir=codeml_dir)
    codeml_file = run_codeml(subdir, ali_x, ali_y)
    codeml_values_dict = parse_codeml_output(codeml_file)
    return codeml_values_dict


def _tables_for_split_alignments(split_ortholog_alignments, ortholog_gene_names, orth_phipack_values):
    """Calculate full tables of values for """
    #Create temporary folder for codeml files
    codeml_dir = tempfile.mkdtemp(prefix='codeml_')
    #Run codeml calculations per sico
    for ortholog, alignx, aligny in split_ortholog_alignments:
        values = _codeml_values_for_alignments(codeml_dir, alignx, aligny)
        orth_phipack_values[ortholog].update(values)
    #Remove codeml_dir
    shutil.rmtree(codeml_dir)

    #Extract ortholog name and correct alignments from split_alignments
    alignments_a = [itemgetter(0, 1)(split_alignment) for split_alignment in split_ortholog_alignments]
    alignments_b = [itemgetter(0, 2)(split_alignment) for split_alignment in split_ortholog_alignments]

    #Create separate data table for genome_ids_a and genome_ids_b
    log.info('About to start calculations of %i clade A genomes vs %i clade B', len(alignments_a), len(alignments_b))
    calculations_a = _calculate_for_clade_alignments(alignments_a, orth_phipack_values, ortholog_gene_names)
    log.info('About to start calculations of %i clade B genomes vs %i clade A', len(alignments_b), len(alignments_a))
    calculations_b = _calculate_for_clade_alignments(alignments_b, orth_phipack_values, ortholog_gene_names)
    return calculations_a, calculations_b


def _calculate_for_clade_alignments(alignments_x, ortholog_codeml_values, ortholog_gene_names):
    """Calculate spreadsheet of data for genomes in genome_ids_x using the provided sico files and codeml values."""
    #Create the output file here
    calculations_file = tempfile.mkstemp(suffix='.tsv', prefix='calculations_')[1]

    #Return empty file if genome_ids_x contains no or only a single genome
    nr_of_strains = len(alignments_x[0][1])
    if nr_of_strains <= 1:
        with open(calculations_file, mode='w') as write_handle:
            write_handle.write('#Need at least two genomes to calculate table, but was: {0}\n'.format(nr_of_strains))
        return calculations_file

    #Temp dir for calculations
    run_dir = tempfile.mkdtemp(prefix='calculate_')

    #Determine the maximum number of columns for singleton, doubleton, etc.
    sfs_max_nton = nr_of_strains // 2

    #Write out statistics file header
    _write_output_file_header(calculations_file, sfs_max_nton)

    #Append all computed values to this list, so we can compute sums and mean afterwards
    all_comp_values = []

    #Run calculcations for each sico alignment
    for orthologname, alignment_x in alignments_x:
        print '\n', orthologname
        #Retrieve ortholog codeml values from dictionary based on orthologname key
        codeml_values_dict = ortholog_codeml_values[orthologname]

        #Append gene name
        codeml_values_dict['product'] = ortholog_gene_names[orthologname]

        #Perform calculations for subaligments of each clade, if clade has more than one sequence; skipping outliers
        comp_values = _perform_calculations(alignment_x, codeml_values_dict)
        all_comp_values.append(comp_values)
        _append_statistics(calculations_file, orthologname, comp_values, sfs_max_nton)

    #"Finally, we might need a line which gives the sum for columns 3 to 15 plus the mean of column 16"
    _append_sums_and_dos_average(calculations_file, sfs_max_nton, all_comp_values)

    #Clean up
    shutil.rmtree(run_dir)

    return calculations_file

#Using the standard NCBI Bacterial, Archaeal and Plant Plastid Code translation table (11)
BACTERIAL_CODON_TABLE = CodonTable.unambiguous_dna_by_id.get(CODON_TABLE_ID)


def _four_fold_degenerate_patterns():
    """Find patterns of 4-fold degenerate codons, wherein all third site substitutions code for the same amino acid."""
    letters = BACTERIAL_CODON_TABLE.nucleotide_alphabet.letters
    #Any combination of letters of length two
    for site12 in [''.join(prod) for prod in product(letters, repeat=2)]:
        #4-fold when the length of the unique encoded amino acids for all possible third site nucleotides is exactly 1
        if 1 == len(set([BACTERIAL_CODON_TABLE.forward_table.get(site12 + site3) for site3 in letters])):
            #Add regular expression pattern to the set of patterns
            yield '{0}[{1}]'.format(site12, letters)

FOUR_FOLD_DEGENERATE_PATTERN = '|'.join(_four_fold_degenerate_patterns())


def _get_nton_name(nton, prefix=''):
    """Given the number of strains in which a polymorphism/substitution is found, give the appropriate SFS name."""
    named = {1: 'single', 2: 'double', 3: 'triple', 4: 'quadruple', 5: 'quintuple'}
    middle = named.get(nton, str(nton) + '-')
    return prefix + middle + 'tons'


def _perform_calculations(alignment, codeml_values):
    """Perform actual calculations on the alignment to determine pN, pS, SFS & the number of ignored cases per SICO."""
    synonymous_sfs = {}
    four_fold_syn_sfs = {}
    non_synonymous_sfs = {}
    four_fold_synonymous_sites = 0
    mixed_synonymous_polymorphisms = 0
    multiple_site_polymorphisms = 0

    #Calculate sequence_lengths here so we can handle alignments that are not multiples of three
    sequence_lengths = len(alignment[0]) - len(alignment[0]) % 3
    #Split into codon_alignments
    codon_alignments = (alignment[:, index:index + 3] for index in range(0, sequence_lengths, 3))
    for codon_alignment in codon_alignments:
        #Get string representations of codons for simplicity
        codons = [str(seqr.seq) for seqr in codon_alignment]

        #As per AEW: ignore codons with gaps, and codons with unresolved bases: Basically anything but ACGT
        if 0 < len(''.join(codons).translate(None, 'ACGTactg')):
            continue

        #Skip codons where any of the alignment codons is a stopcodon, same as in codeml
        if any(codon in BACTERIAL_CODON_TABLE.stop_codons for codon in codons):
            continue

        #Retrieve translations of codons now that inconclusive & stop-codons have been removed
        translations = [BACTERIAL_CODON_TABLE.forward_table.get(codon) for codon in codons]

        #Count unique translations across strains
        translation_usage = dict((aa, translations.count(aa)) for aa in set(translations))

        #Mutations are synonymous when all codons encode the same AA, and there are no skipped codons
        synonymous = len(translation_usage) == 1 and len(translations) == len(codon_alignment)

        #Retrieve nucleotides per site within the codon
        site1 = [nucl for nucl in codon_alignment[:, 0]]
        site2 = [nucl for nucl in codon_alignment[:, 1]]
        site3 = [nucl for nucl in codon_alignment[:, 2]]

        #Count occurrences of distinct nucleotides across strains
        site1_usage = dict((nucl, site1.count(nucl)) for nucl in set(site1))
        site2_usage = dict((nucl, site2.count(nucl)) for nucl in set(site2))
        site3_usage = dict((nucl, site3.count(nucl)) for nucl in set(site3))

        #Sites are polymorphic if they contain more than one nucleotide
        site1_polymorphic = 1 < len(site1_usage)
        site2_polymorphic = 1 < len(site2_usage)
        site3_polymorphic = 1 < len(site3_usage)
        polymorphisms = site1_polymorphic, site2_polymorphic, site3_polymorphic

        #Continue with next codon if none of the sites is polymorphic
        if not any(polymorphisms):
            #But do increase the number of 4-fold synonymous sites if the pattern matches
            codon = codons[0]
            if re.match(FOUR_FOLD_DEGENERATE_PATTERN, codon):
                #Increase by one, as this site is for fold degenerate, even if it is not polymorphic
                four_fold_synonymous_sites += 1
            continue

        #Determine if only one site is polymorphic by using boolean xor and not all
        single_site_polymorphism = site1_polymorphic ^ site2_polymorphic ^ site3_polymorphic and not all(polymorphisms)

        #Skip multiple site polymorphisms, but do keep a count of how many we encounter
        if not single_site_polymorphism:
            multiple_site_polymorphisms += 1
            continue

        #Determine which site_usage is the single site polymorphism
        polymorph_site_usage = site1_usage if site1_polymorphic else site2_usage if site2_polymorphic else site3_usage

        #Find the 'reference' nucleotide as (one of) the most occurring occupations in this site, so we can -1 later
        psu_values = polymorph_site_usage.values()
        reference_allele_count = max(psu_values)

        #Calculate the local site frequency spectrum, to be added to the gene-wide SFS later
        #We'll be using Site Frequency Spectrum to calculate the number of synonymous and non synonymous polymorphisms
        #Note: this requires a complete SFS across synonymous & non_synonymous polymorphisms, be careful when updating
        local_sfs = dict((ntimes, psu_values.count(ntimes)) for ntimes in set(psu_values))
        #Deduct one for the reference_allele_count, which should not count towards the SFS
        local_sfs[reference_allele_count] = local_sfs[reference_allele_count] - 1
        #Remove empty value as possible result of the above decrement operation
        if local_sfs[reference_allele_count] == 0:
            del local_sfs[reference_allele_count]

        def _update_sfs_with_local_sfs(sfs, local_sfs):
            """Add values from local_sfs to gene-wide sfs"""
            for maf, count in local_sfs.iteritems():
                prev_occupations = sfs.get(maf, 0)
                sfs[maf] = prev_occupations + count

        if synonymous:
            #If all polymorphisms encode for the same AA, we have multiple synonymous polymorphisms, where:
            #2 nucleotides = 1 polymorphism, 3 nucleotides = 2 polymorphisms, 4 nucleotides = 3 polymorphisms

            #Update synonymous SFS by adding values from local SFS
            _update_sfs_with_local_sfs(synonymous_sfs, local_sfs)

            #Codon is four fold degenerate if it matches FOUR_FOLD_DEGENERATE_PATTERN
            if site3_polymorphic:
                codon = codons[0]
                if re.match(FOUR_FOLD_DEGENERATE_PATTERN, codon):
                    #Update four fold degenerate SFS by adding values from local SFS
                    _update_sfs_with_local_sfs(four_fold_syn_sfs, local_sfs)
                    #Increase the number of four_fold synonymous sites here as well
                    four_fold_synonymous_sites += 1
        else:  #not synonymous
            if len(polymorph_site_usage) == len(translation_usage):
                #If all polymorphisms encode for different AA, we have multiple non-synonymous polymorphisms, where:
                #2 nucleotides = 1 polymorphism, 3 nucleotides = 2 polymorphisms, 4 nucleotides = 3 polymorphisms

                #Update non synonymous SFS by adding values from local SFS
                _update_sfs_with_local_sfs(non_synonymous_sfs, local_sfs)
            else:
                #Some, but not all polymorphisms encode for different AA, making it unclear how this should be scored
                mixed_synonymous_polymorphisms += 1

    #Compute combined values from the above counted statistics
    computed_values = _compute_values_from_statistics(len(alignment), sequence_lengths, codeml_values,
                                                      synonymous_sfs, non_synonymous_sfs, four_fold_syn_sfs,
                                                      four_fold_synonymous_sites)

    #Miscellaneous additional values
    computed_values['codons'] = sequence_lengths // 3
    computed_values['multiple site polymorphisms'] = multiple_site_polymorphisms
    computed_values['complex codons (with both synonymous and non-synonymous polymorphisms segregating)'] = mixed_synonymous_polymorphisms

    #Add COGs to output file in split columns
    cog_digits = []
    cog_letters = []
    for cog in find_cogs_in_sequence_records(alignment):
        matchobj = re.match('(COG[0-9]+)([A-Z]*)', cog)
        if matchobj:
            cog_digits.append(matchobj.groups()[0])
            cog_letters.append(matchobj.groups()[1])
    computed_values['cog digits'] = ','.join(cog_digits)
    computed_values['cog letters'] = ','.join(cog_letters)

    return computed_values


def _calc_pi(nr_of_strains, sequence_lengths, site_freq_spec):
    """
    New, improved: n/(n-1) * Sum( Pj * 2 * j/n * (1-j/n), {i,1,Floor((n-1)/2)})
    
    Pi: n/(n-1) * Sum[ D(i) * 2 i/n (1-i/n), i from 1 to RoundDown(n/2)]
    where n is number of strains
    and D(i) is the number of polymorphisms present in i of n strains
    finally divide everything by the number of sites
    """
    print '\n', site_freq_spec
    return (nr_of_strains
                     / (nr_of_strains - 1)
                     * sum(site_freq_spec.get(i, 0)
                           * 2 * i / nr_of_strains
                           * (1 - i / nr_of_strains)
                           for i in range(1, (nr_of_strains - 1) // 2 + 1))  #+1 as range excludes stop value
                     ) / sequence_lengths


def _compute_values_from_statistics(nr_of_strains, sequence_lengths, codeml_values,
                                    synonymous_sfs, non_synonymous_sfs, four_fold_syn_sfs, four_fold_synonymous_sites):
    """Compute values (mostly from the site frequency spectra) that we'll output according to provided formula's."""
    #12. number of non-synonymous sites for divergence from PAML
    #(this might be different to 3, because this will come from two randomly chosen sequences) = LnD
    paml_non_synonymous_sites = codeml_values['N']

    #14. number of synonymous sites from PAML = LsD
    paml_synonymous_sites = codeml_values['S']

    #Dictionary to hold computed values
    calc_values = {}

    #Add all values in codeml_values to calc_values
    calc_values.update(codeml_values)

    def _add_sfs_values_in_columns(sfs_name, sfs):
        """Add values contained within SFS to named columns for singletons, doubletons, tripletons, etc.."""
        max_nton = nr_of_strains // 2 + 1
        for nton in range(1, max_nton):
            name = _get_nton_name(nton, prefix=sfs_name)
            value = sfs[nton] if nton in sfs else 0
            calc_values[name] = value

    #3. number of non-synonymous sites (see below for calculation) = LnP = L * LnD/(LnD+LsD)
    #where L is the length of the sequence from which the polymorphism data is taken (i.e. 3* no of codons used)
    calc_values['non-synonymous sites'] = sequence_lengths * paml_non_synonymous_sites / (paml_non_synonymous_sites +
                                                                                          paml_synonymous_sites)

    #4. SFS for non-synonymous polymorphsims
    _add_sfs_values_in_columns('non-synonymous sfs ', non_synonymous_sfs)

    #5. The sum of SFS for non-synonymous polymorphisms = Pn
    calc_values['non-synonymous polymorphisms'] = non_synonymous_polymorphisms = sum(non_synonymous_sfs.values())

    #6. number of synonymous sites (see below for calculation) = LsP = L * LsD/(LnD+LsD)
    #where L is the length of the sequence from which the polymorphism data is taken (i.e. 3* no of codons used)
    calc_values['synonymous sites'] = sequence_lengths * paml_synonymous_sites / (paml_non_synonymous_sites +
                                                                                  paml_synonymous_sites)

    #7. SFS for synonymous polymorphisms
    _add_sfs_values_in_columns('synonymous sfs ', synonymous_sfs)

    #8. The sum of the SFS for synonymous polymorphisms = Ps
    calc_values['synonymous polymorphisms'] = synonymous_polymorphisms = sum(synonymous_sfs.values())

    #9. number of 4-fold synonymous sites = L4  (all degenerate sites, _including_ those that are not polymorphic)
    calc_values['4-fold synonymous sites'] = four_fold_synonymous_sites

    #10. SFS for 4-fold synonymous polymorphisms
    _add_sfs_values_in_columns('4-fold synonymous sfs ', four_fold_syn_sfs)

    #11. Sum of the SFS for 4-folds = P4
    calc_values['4-fold synonymous polymorphisms'] = sum(four_fold_syn_sfs.values())

    #13. number of non-synonymous substitutions from PAML = Dn
    paml_non_synonymous_substitut = codeml_values['Dn']

    #15. number of synonymous substitutions from PAML = Ds
    paml_synonymous_substitutions = codeml_values['Ds']

    #16. Direction of selection = Dn/(Dn+Ds) - Pn/(Pn+Ps)
    paml_total_substitutions = paml_non_synonymous_substitut + paml_synonymous_substitutions
    total_polymorphisms = non_synonymous_polymorphisms + synonymous_polymorphisms
    #Prevent divide by zero by checking both values above are not null
    if paml_total_substitutions != 0 and total_polymorphisms != 0:
        calc_values['DoS'] = (paml_non_synonymous_substitut / paml_total_substitutions
                                                 - non_synonymous_polymorphisms / total_polymorphisms)
    else:
        #"the direction of selection is undefined if either Dn+Ds or Pn+Ps are zero": None or Not a Number?
        calc_values['DoS'] = None

    #Combine synonymous and non synonymous site frequency spectra to get polymorphism sfs for below Pi calculation
    polymorpisms_sfs = synonymous_sfs.copy()
    for key, value in non_synonymous_sfs.iteritems():
        #Merge values such that their values are added up when a conflicting key is found
        polymorpisms_sfs[key] = polymorpisms_sfs.get(key, 0) + value

    #TODO Remove print statements here, and above
    print 'nr_of_strains', nr_of_strains
    print 'sequence_lengths', sequence_lengths

    calc_values['Pi'] = _calc_pi(nr_of_strains, sequence_lengths, polymorpisms_sfs)
    print 'Pi', calc_values['Pi']

    calc_values['Pi nonsyn'] = _calc_pi(nr_of_strains, sequence_lengths, non_synonymous_sfs)
    print 'Pi nonsyn', calc_values['Pi nonsyn']

    calc_values['Pi syn'] = _calc_pi(nr_of_strains, sequence_lengths, synonymous_sfs)
    print 'Pi syn', calc_values['Pi syn']

    calc_values['Pi 4-fold syn'] = _calc_pi(nr_of_strains, sequence_lengths, four_fold_syn_sfs)
    print 'Pi 4-fold syn', calc_values['Pi 4-fold syn']

    #Watterson's estimator of theta: S / (L * harmonic)
    #where the harmonic is Sum[ 1 / i, i from 1 to n - 1 ]
    seg_sites = sum(polymorpisms_sfs.get(i, 0) for i in range(1, nr_of_strains // 2 + 1))
    harmonic = sum(1 / i for i in range(1, nr_of_strains))  #Not +/-1 as range already excludes the stop value
    calc_values['Theta'] = seg_sites / (sequence_lengths * harmonic)

    #These values will end up contributing to the Neutrality Index through NI = Sum(X) / Sum(Y)
    ps_plus_ds = (synonymous_polymorphisms + paml_synonymous_substitutions)
    if ps_plus_ds:
        #X = Ds*Pn/(Ps+Ds)
        calc_values['Ds*Pn/(Ps+Ds)'] = paml_synonymous_substitutions * non_synonymous_polymorphisms / ps_plus_ds
        #Y = Dn*Ps/(Ps+Ds)
        calc_values['Dn*Ps/(Ps+Ds)'] = paml_non_synonymous_substitut * synonymous_polymorphisms / ps_plus_ds
    else:
        calc_values['Ds*Pn/(Ps+Ds)'] = None
        calc_values['Dn*Ps/(Ps+Ds)'] = None

    return calc_values


def _get_column_headers_in_sequence(max_nton):
    """Return the column headers to the generated statistics files in the correct order, using max_nton for SFS max."""
    #Gene name is captured as product of the orthologous DNA sequence
    headers = ['product']

    #Split COG into first part with numbers and second part with letters
    headers.extend(['cog digits', 'cog letters'])
    #Some initial values
    headers.append('codons')

    def _write_sfs_column_names(prefix):
        """Return named columns for SFS singleton, doubleton, tripleton, etc.. upto max_nton."""
        for number in range(1, max_nton + 1):
            yield _get_nton_name(number, prefix)

    #Non synonymous
    headers.extend(['non-synonymous sites', 'non-synonymous polymorphisms'])
    headers.extend(_write_sfs_column_names('non-synonymous sfs '))
    #Synonymous
    headers.extend(['synonymous sites', 'synonymous polymorphisms'])
    headers.extend(_write_sfs_column_names('synonymous sfs '))
    #4-fold synonymous
    headers.extend(['4-fold synonymous sites', '4-fold synonymous polymorphisms'])
    headers.extend(_write_sfs_column_names('4-fold synonymous sfs '))
    #Miscellaneous additional statistics
    headers.append('multiple site polymorphisms')
    headers.append('complex codons (with both synonymous and non-synonymous polymorphisms segregating)')
    #PAML
    headers.extend(['N', 'Dn', 'S', 'Ds'])
    #PhiPack
    headers.extend(['PhiPack sites', 'Phi', 'Max Chi^2', 'NSS'])

    #Measures of nucleotide diversity per SICO
    headers.extend(['Pi',
                    'Pi nonsyn',
                    'Pi syn',
                    'Pi 4-fold syn',
                    'Theta'])

    #Hide the following columns in the output, but do calculate & pass their values
    headers.append('Ds*Pn/(Ps+Ds)')
    headers.append('Dn*Ps/(Ps+Ds)')

    #Final _two_ values here are ignored when calculation sums over complete table
    headers.append('neutrality index')
    headers.append('DoS')

    return headers


def _write_output_file_header(calculations_file, max_nton):
    """Write header line for combined statistics file."""
    with open(calculations_file, mode='a') as append_handle:
        append_handle.write('#ortholog\t')
        append_handle.write('\t'.join(column for column in _get_column_headers_in_sequence(max_nton)
                                      #Hide the following columns in the output, but do calculate & pass their values
                                      if column not in ('Ds*Pn/(Ps+Ds)', 'Dn*Ps/(Ps+Ds)', 'N', 'S')))
        append_handle.write('\n')


def _append_statistics(calculations_file, orthologname, comp_values, max_nton):
    """Append statistics for individual ortholog to genome-wide files."""
    with open(calculations_file, mode='a') as append_handle:
        append_handle.write(orthologname + '\t')
        append_handle.write('\t'.join(str(comp_values.get(column, ''))
                                      for column in _get_column_headers_in_sequence(max_nton)
                                      #Hide the following columns in the output, but do calculate & pass their values
                                      if column not in ('Ds*Pn/(Ps+Ds)', 'Dn*Ps/(Ps+Ds)', 'N', 'S')))
        append_handle.write('\n')


def _prepend_table_header(table_file, genomes_x, common_prefix_x, genomes_y, common_prefix_y, oddeven):
    """Prepend header to output data table that lists source IDs"""
    with open(table_file, mode='w') as write_handle:
        #Write out the number of strains for each of the taxa, and the participating IDs
        write_handle.write('#{0} {1} strains compared with {2} {3} strains\n'.format(len(genomes_x),
                                                                                   common_prefix_x,
                                                                                   len(genomes_y),
                                                                                   common_prefix_y))
        write_handle.write('#IDs {0}: {1}\n'.format(common_prefix_x, ', '.join(sorted(genomes_x))))
        write_handle.write('#IDs {0}: {1}\n'.format(common_prefix_y, ', '.join(sorted(genomes_y))))

        #Explain the presence of three tables in one file if we're also calculating odd & even codons
        if oddeven:
            write_handle.write('#First table contains calculations for all codon\n')
            write_handle.write('#Second table contains calculations for odd codons only\n')
            write_handle.write('#Third table contains calculations for even codons only\n')

        #Possible extension: #orthologs dropped in each step, #cut-off used to trim alignments, #of strains


def main(args):
    """Main function called when run from command line or as part of pipeline."""
    usage = """
Usage: calculations.py
--genomes-a=FILE     file with GenBank Project IDs from complete genomes table on each line for taxon A
--genomes-b=FILE     file with GenBank Project IDs from complete genomes table on each line for taxon B
--sico-zip=FILE      archive of aligned & trimmed single copy orthologous (SICO) genes
--table-a=FILE       destination file path for summary statistics table based on orthologs in taxon A
--table-b=FILE       destination file path for summary statistics table based on orthologs in taxon B
--append-odd-even    append separate tables calculated for odd and even codons of ortholog alignments [OPTIONAL]
"""
    options = ['genomes-a', 'genomes-b', 'sico-zip', 'table-a', 'table-b', 'append-odd-even?']
    genome_a_ids_file, genome_b_ids_file, sico_zip, table_a, table_b, oddeven = parse_options(usage, options, args)

    #Parse file containing GenBank GenBank Project IDs to extract GenBank Project IDs
    with open(genome_a_ids_file) as read_handle:
        lines = [line.strip() for line in read_handle]
        genome_ids_a = [line.split()[0] for line in lines]
        common_prefix_a = os.path.commonprefix([line.split('\t')[1] for line in lines]).strip()
    with open(genome_b_ids_file) as read_handle:
        lines = [line.strip() for line in read_handle]
        genome_ids_b = [line.split()[0] for line in lines]
        common_prefix_b = os.path.commonprefix([line.split('\t')[1] for line in lines]).strip()

    #Prepend headers to each of the output tables
    _prepend_table_header(table_a, genome_ids_a, common_prefix_a, genome_ids_b, common_prefix_b, oddeven)
    _prepend_table_header(table_b, genome_ids_b, common_prefix_b, genome_ids_a, common_prefix_a, oddeven)

    #Create run_dir to hold files relating to this run
    run_dir = tempfile.mkdtemp(prefix='calculations_')

    #Extract files from zip archive
    sico_files = extract_archive_of_files(sico_zip, create_directory('sicos', inside_dir=run_dir))

    #Actually do calculations
    tmp_table_tuple = calculate_tables(genome_ids_a, genome_ids_b, sico_files, oddeven)

    #Write the produced files to command line argument filenames
    with open(table_a, mode='ab') as append_handle:
        shutil.copyfileobj(open(tmp_table_tuple[0], mode='rb'), append_handle)
    with open(table_b, mode='ab') as append_handle:
        shutil.copyfileobj(open(tmp_table_tuple[1], mode='rb'), append_handle)

    #Remove now unused files to free disk space
    shutil.rmtree(run_dir)
    os.remove(tmp_table_tuple[0])
    os.remove(tmp_table_tuple[1])

    #Exit after a comforting log message
    log.info("Produced: \n%s\n%s", table_a, table_b)

if __name__ == '__main__':
    main(sys.argv[1:])
